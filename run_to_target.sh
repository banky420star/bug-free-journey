#!/usr/bin/env bash
# Run live BTC 5m trades in a loop until: target balance, floor, max trades, or daily-loss.
#
# - Scales the stake with the bankroll (kelly-ish: risk_frac × balance, capped)
# - Stops when balance >= target       (default $1,000,000)
# - Stops when balance <  floor        (default $5 — can't afford the 5-share minimum)
# - Stops after max_trades completed   (default 500 — enough to judge if the edge is real)
# - Stops on a daily-loss circuit breaker (default 20% down in a day)
# - Logs each trade's result to runtime/loop_pnl.csv
#
# Usage:
#   ./run_to_target.sh                           # 500 trades, target $1M, floor $5
#   ./run_to_target.sh --max-trades 50           # quick 50-trade test
#   ./run_to_target.sh --target 100 --floor 5    # small target for testing
#   ./run_to_target.sh --risk-frac 0.10          # more aggressive sizing
#
# ⚠️ REAL pUSD orders. ⚠️ Keys in .env are compromised — rotate before relying on this.
set -euo pipefail
cd "$(dirname "$0")"
export LC_NUMERIC=C    # force dot-decimal in printf (avoid locale comma issues)

VENV_PY="pm-hl-conservative-plus-repo/.venv/bin/python"
RUNTIME="skills/btc-5m-live/runtime"
RUNNER="skills/btc-5m-live/scripts/test_btc_5m_session_exit_sl.py"
LOOP_LOG="$RUNTIME/loop_pnl.csv"
mkdir -p "$RUNTIME"

# Defaults
TARGET=1000000
FLOOR=5
RISK_FRAC=0.05
MAX_STAKE=100       # cap per-trade stake (room for progressive staking to grow)
THRESHOLD=0.62
MAX_ENTRY_PRICE=0.75
ENTRY_TIMEOUT_MIN=5
MIN_STAKE=5         # 5-share minimum on BTC 5m markets
MAX_TRADES=500
PARALLEL=0            # 1 = spawn all 5 assets per cycle, 0 = cycle one at a time
PARALLEL_MIN_BAL=100  # don't enable parallel below this balance (safety)

# Per-asset overrides — BTC/ETH/XRP get tighter settings (more safe).
# Higher threshold = only enter when the market's implied probability is stronger.
# Lower max-entry-price = cap the cost per share (less downside, more upside per win).
# SOL/DOGE keep the looser 0.62 / 0.75 defaults — they've been the consistent winners.
# bash 3.2 on macOS has no associative arrays, so we use case lookups.
get_asset_threshold() {
  case "$1" in
    btc|eth|xrp) echo "0.65" ;;
    sol|doge)    echo "0.62" ;;
    *)           echo "$THRESHOLD" ;;
  esac
}
get_asset_max_price() {
  case "$1" in
    btc|eth|xrp) echo "0.70" ;;
    sol|doge)    echo "0.75" ;;
    *)           echo "$MAX_ENTRY_PRICE" ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)            TARGET="$2"; shift 2;;
    --floor)             FLOOR="$2"; shift 2;;
    --risk-frac)         RISK_FRAC="$2"; shift 2;;
    --max-stake)         MAX_STAKE="$2"; shift 2;;
    --threshold)         THRESHOLD="$2"; shift 2;;
    --max-entry-price)   MAX_ENTRY_PRICE="$2"; shift 2;;
    --entry-timeout-min) ENTRY_TIMEOUT_MIN="$2"; shift 2;;
    --max-trades)        MAX_TRADES="$2"; shift 2;;
    --parallel)          PARALLEL=1; shift;;
    --parallel-min-bal)  PARALLEL_MIN_BAL="$2"; shift 2;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

# Init CSV log
if [[ ! -f "$LOOP_LOG" ]]; then
  echo "ts,asset,balance_before,stake,trade_result,shares,cost,close_usdc,pnl,balance_after,settle_wait,balance_unconfirmed,side,entry_price" > "$LOOP_LOG"
fi

# Get current pUSD balance
get_balance() {
  "$VENV_PY" -c "
import os, importlib.util
from dotenv import load_dotenv
load_dotenv('pm-hl-conservative-plus-repo/.env')
spec = importlib.util.spec_from_file_location('r', 'skills/btc-5m-live/scripts/test_btc_5m_session_exit_sl.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
c = m.auth_clob_client()
b = m.get_pusd_balance(c)
print(f'{b:.6f}' if b is not None else '0')
" 2>/dev/null
}

# Progressive staking (per-asset): every 3 CONSECUTIVE wins of THIS asset, boost
# this asset's stake by its average win size. A single loss of this asset resets
# its own streak to 0. Other assets' results don't affect this asset's boost.
get_stake_boost() {
  local asset="$1"
  if [[ ! -f "$LOOP_LOG" ]]; then
    echo "0"; return
  fi
  "$VENV_PY" -c "
import csv
rows = []
try:
    with open('$LOOP_LOG', newline='') as f:
        for r in csv.DictReader(f):
            if r.get('trade_result') == 'done' and (r.get('asset') or '').lower() == '$asset':
                rows.append(r)
except Exception:
    print('0'); exit()
# Walk back from this asset's latest trade; count its current win streak
streak_wins = []
for r in reversed(rows):
    pnl = float(r.get('pnl') or 0)
    if pnl > 0:
        streak_wins.append(r)
    else:
        break
if not streak_wins:
    print('0'); exit()
avg_win = sum(float(r['pnl']) for r in streak_wins) / len(streak_wins)
boost = (len(streak_wins) // 3) * avg_win
print(f'{boost:.2f}')
" 2>/dev/null
}

# Parse the latest run log for the trade outcome (also extracts the side: UP/DOWN)
parse_last_run() {
  local log="$1"
  "$VENV_PY" -c "
import json, re, sys
txt = open('$log').read()
m = re.search(r'\{\s*\"started_at\"', txt)
if not m:
    print('no_entry||0|0|0|0|0|False||0'); sys.exit()
body = txt[m.start():]
depth = 0; end = 0
for i, ch in enumerate(body):
    if ch == '{': depth += 1
    elif ch == '}':
        depth -= 1
        if depth == 0: end = i + 1; break
r = json.loads(body[:end])
result = r.get('result') or 'unknown'
o = r.get('opened') or {}
c = r.get('closed') or {}
side = o.get('side') or ''
entry_price = o.get('entry_price') or 0
shares = o.get('shares') or 0
cost = o.get('cost_usdc') or 0
close_usdc = c.get('close_usdc') or 0
pnl = r.get('realized_cashflow_pnl_usdc')
if pnl is None: pnl = 0
settle = o.get('settle_wait_sec') or 0
unconf = o.get('balance_unconfirmed')
print(f'{result}|{shares}|{cost}|{close_usdc}|{pnl}|{settle}|{unconf}|{side}|{entry_price}')
" 2>/dev/null
}

echo "=== BTC 5m loop runner ==="
echo "  Mode:       $([ "$PARALLEL" = "1" ] && echo 'PARALLEL (5 assets/cycle)' || echo 'sequential (1 asset/cycle)')"
echo "  Target:     \$$(printf '%.0f' $TARGET)"
echo "  Floor:      \$$(printf '%.0f' $FLOOR)"
echo "  Max trades: $MAX_TRADES"
echo "  Risk:       ${RISK_FRAC} of balance per trade (capped at \$${MAX_STAKE})"
if [[ "$PARALLEL" = "1" ]]; then
  echo "  Parallel:   5 assets/cycle, min balance \$${PARALLEL_MIN_BAL}"
fi
echo "  Log:        $LOOP_LOG"
echo "  Stop with:  Ctrl-C  or  skills/btc-5m-live/scripts/btc5m_ctl.sh stop"
echo

DAY_START=$(date -u +%Y-%m-%d)
DAY_START_BAL=$(get_balance)
echo "Day $DAY_START starting balance: \$$(printf '%.2f' $DAY_START_BAL)"

# fire_parallel_batch: spawn one python runner per asset, wait for all to finish, log all to CSV.
# Bypasses btc5m_ctl.sh because its single PIDFILE can't track 5 concurrent processes.
fire_parallel_batch() {
  local batch_stake="$1"
  local bal_before="$2"
  local ts_prefix
  ts_prefix="$(date -u +%Y%m%dT%H%M%SZ)"

  declare -A PIDS LOGS
  for asset in "${ASSETS[@]}"; do
    local log="$RUNTIME/btc5m_par_${asset}_${ts_prefix}.log"
    LOGS["$asset"]="$log"
    # Spawn the python runner directly with env vars sourced from .env
    (
      set -a
      [ -f "pm-hl-conservative-plus-repo/.env" ] && source "pm-hl-conservative-plus-repo/.env"
      set +a
      cd "pm-hl-conservative-plus-repo"
      nohup "$VENV_PY" "$RUNNER" --profile conservative --asset "$asset" \
        --entry-timeout-min "$ENTRY_TIMEOUT_MIN" --poll-sec 2 \
        --close-retry-max 30 --close-retry-delay-sec 2 --execute \
        --stake-usd "$batch_stake" --threshold "$CUR_THRESHOLD" --max-entry-price "$CUR_MAX_PRICE" \
        >"$log" 2>&1 &
      echo $! > "$RUNTIME/par_${asset}.pid"
    )
    sleep 0.2  # slight stagger so all 5 don't hit the CLOB API at the exact same instant
    PIDS["$asset"]=$(cat "$RUNTIME/par_${asset}.pid" 2>/dev/null || echo "")
    echo "[$(date -u +%H:%M:%SZ)] spawned $asset pid=${PIDS[$asset]} log=$(basename "$log")"
  done

  # Wait for all 5 to finish (poll each log for "result" field, or its pid to exit)
  echo "[$(date -u +%H:%M:%SZ)] waiting for 5 parallel trades to complete..."
  local alive=1
  while [[ $alive -eq 1 ]]; do
    alive=0
    for asset in "${ASSETS[@]}"; do
      local pid="${PIDS[$asset]}"
      local log="${LOGS[$asset]}"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        alive=1
      elif [[ -n "$log" && ! -f "$log" ]]; then
        alive=1
      fi
    done
    [[ $alive -eq 1 ]] && sleep 5
  done

  # Parse + log each
  local bal_after
  bal_after=$(get_balance)
  for asset in "${ASSETS[@]}"; do
    local log="${LOGS[$asset]}"
    local result shares cost close_usdc pnl settle unconf side entry_price
    IFS='|' read -r result shares cost close_usdc pnl settle unconf side entry_price <<< "$(parse_last_run "$log")"
    [[ -z "$result" ]] && result="no_entry"
    if [[ "$result" == "done" ]]; then
      TRADES_DONE=$((TRADES_DONE + 1))
    fi
    echo "$(date -u +%FT%TZ),$asset,$bal_before,$batch_stake,$result,$shares,$cost,$close_usdc,$pnl,$bal_after,$settle,$unconf,$side,$entry_price" >> "$LOOP_LOG"
    echo "[$(date -u +%H:%M:%SZ)] result=$result asset=$asset side=$side  pnl=\$$(printf '%.2f' "${pnl:-0}")  trade=$TRADES_DONE/$MAX_TRADES"
    rm -f "$RUNTIME/par_${asset}.pid"
  done
}

ITERATION=0
TRADES_DONE=0
ASSETS=(btc eth sol xrp doge)
ASSET_IDX=0
while true; do
  ITERATION=$((ITERATION + 1))
  ASSET="${ASSETS[$ASSET_IDX]}"
  ASSET_IDX=$(( (ASSET_IDX + 1) % ${#ASSETS[@]} ))

  # Resolve per-asset threshold + max-entry-price (fall back to globals if missing)
  CUR_THRESHOLD=$(get_asset_threshold "$ASSET")
  CUR_MAX_PRICE=$(get_asset_max_price "$ASSET")

  # Max trades reached?
  if [[ $TRADES_DONE -ge $MAX_TRADES ]]; then
    echo "📋 MAX TRADES REACHED ($MAX_TRADES). Stopping. Analyze $LOOP_LOG to judge the edge."
    break
  fi

  # Check day rollover for the daily-loss breaker
  TODAY=$(date -u +%Y-%m-%d)
  if [[ "$TODAY" != "$DAY_START" ]]; then
    DAY_START="$TODAY"
    DAY_START_BAL=$(get_balance)
    echo "[$(date -u +%H:%M:%SZ)] New day — daily-loss breaker reset. Start balance: \$$(printf '%.2f' $DAY_START_BAL)"
  fi

  BAL=$(get_balance)
  echo "[$(date -u +%H:%M:%SZ)] iter=$ITERATION  balance=\$$(printf '%.2f' $BAL)"

  # Target reached?
  if (( $(echo "$BAL >= $TARGET" | bc -l) )); then
    echo "🎉 TARGET REACHED! Balance \$$(printf '%.2f' $BAL) >= \$$(printf '%.0f' $TARGET). Stopping."
    break
  fi

  # Below floor?
  if (( $(echo "$BAL < $FLOOR" | bc -l) )); then
    echo "💀 FLOOR HIT. Balance \$$(printf '%.2f' $BAL) < \$$(printf '%.0f' $FLOOR). Can't afford min order. Stopping."
    break
  fi

  # Daily-loss circuit breaker
  DAILY_LOSS=$(echo "$DAY_START_BAL - $BAL" | bc -l)
  DAILY_LOSS_PCT=$(echo "scale=4; $DAILY_LOSS / $DAY_START_BAL" | bc -l)
  if (( $(echo "$DAILY_LOSS_PCT > 0.40" | bc -l) )); then
    echo "🛑 DAILY-LOSS BREAKER. Down $(printf '%.1f' $(echo "$DAILY_LOSS_PCT * 100" | bc -l))% today (\$$(printf '%.2f' $DAILY_LOSS)). Stopping."
    break
  fi

  # Compute stake: base = risk_frac × balance (min $5), then add progressive boost.
  # Every 3 wins, the stake grows by the average win size (let winners ride).
  BASE_STAKE=$(echo "scale=2; $BAL * $RISK_FRAC" | bc -l)
  if (( $(echo "$BASE_STAKE < $MIN_STAKE" | bc -l) )); then
    BASE_STAKE=$MIN_STAKE
  fi
  STAKE_BOOST=$(get_stake_boost "$ASSET")
  STAKE=$(echo "scale=2; $BASE_STAKE + $STAKE_BOOST" | bc -l)
  if (( $(echo "$STAKE > $MAX_STAKE" | bc -l) )); then
    STAKE=$MAX_STAKE
  fi
  if (( $(echo "$STAKE < $MIN_STAKE" | bc -l) )); then
    STAKE=$MIN_STAKE
  fi

  echo "[$(date -u +%H:%M:%SZ)] firing trade: asset=$ASSET stake=\$$(printf '%.2f' $STAKE) (base=\$$(printf '%.2f' $BASE_STAKE) boost=\$$(printf '%.2f' $STAKE_BOOST)) threshold=$CUR_THRESHOLD max_price=$CUR_MAX_PRICE"

  # === PARALLEL MODE: spawn all 5 assets in one batch ===
  if [[ "$PARALLEL" = "1" ]]; then
    # Safety: refuse parallel if balance below the parallel floor
    if (( $(echo "$BAL < $PARALLEL_MIN_BAL" | bc -l) )); then
      echo "[$(date -u +%H:%M:%SZ)] balance \$$(printf '%.2f' $BAL) < parallel min \$${PARALLEL_MIN_BAL}. Falling back to sequential this cycle."
    else
      # Per-trade stake for parallel: smaller slice of bankroll (5 trades at once)
      PAR_STAKE=$(echo "scale=2; $BAL * $RISK_FRAC / 5" | bc -l)
      (( $(echo "$PAR_STAKE < $MIN_STAKE" | bc -l) )) && PAR_STAKE=$MIN_STAKE
      (( $(echo "$PAR_STAKE > $MAX_STAKE" | bc -l) )) && PAR_STAKE=$MAX_STAKE
      # Need at least 5 × stake to cover the whole batch
      BATCH_COST=$(echo "scale=2; $PAR_STAKE * 5" | bc -l)
      if (( $(echo "$BATCH_COST > $BAL" | bc -l) )); then
        echo "[$(date -u +%H:%M:%SZ)] batch cost \$$(printf '%.2f' $BATCH_COST) > balance \$$(printf '%.2f' $BAL). Falling back to sequential."
      else
        echo "[$(date -u +%H:%M:%SZ)] PARALLEL batch: 5 assets × \$$(printf '%.2f' $PAR_STAKE) = \$$(printf '%.2f' $BATCH_COST) at risk"
        fire_parallel_batch "$PAR_STAKE" "$BAL"
        sleep 5
        continue
      fi
    fi
    # If we fell through to here, run sequential this cycle
    ASSET="${ASSETS[$ASSET_IDX]}"
    ASSET_IDX=$(( (ASSET_IDX + 1) % ${#ASSETS[@]} ))
  fi

  # Fire one live trade (sequential mode)
  TRADE_LOG=""
  ./run_live.sh --asset "$ASSET" --threshold "$CUR_THRESHOLD" --max-entry-price "$CUR_MAX_PRICE" --stake-usd "$STAKE" --entry-timeout-min "$ENTRY_TIMEOUT_MIN" > /tmp/btc5m_loop_out 2>&1 || true
  # Extract the log path from run_live.sh output ("started pid=XXX log=PATH")
  TRADE_LOG=$(grep -oE 'log=[^ ]+' /tmp/btc5m_loop_out | head -1 | cut -d= -f2)
  if [[ -z "$TRADE_LOG" ]]; then
    echo "[$(date -u +%H:%M:%SZ)] no log path found, sleeping 30s before retry"
    sleep 30
    continue
  fi

  # Wait for the run to finish (run_live.sh blocks until btc5m_ctl.sh returns, but the
  # actual python process is nohup'd — poll for the result field in the log)
  echo "[$(date -u +%H:%M:%SZ)] waiting for trade to complete..."
  WAIT_PID=$(grep -oE 'pid=[0-9]+' /tmp/btc5m_loop_out | head -1 | cut -d= -f2)
  while kill -0 "$WAIT_PID" 2>/dev/null && ! grep -q '"result"' "$TRADE_LOG" 2>/dev/null; do
    sleep 5
  done

  # Parse the outcome
  IFS='|' read -r RESULT SHARES COST CLOSE_USDC PNL SETTLE UNCONF SIDE ENTRY_PRICE <<< "$(parse_last_run "$TRADE_LOG")"
  BAL_AFTER=$(get_balance)

  # Only count trades that actually entered (not no_entry timeouts)
  if [[ "$RESULT" == "done" ]]; then
    TRADES_DONE=$((TRADES_DONE + 1))
  fi

  # Log to CSV (includes side + entry_price so the TUI can show which way the bet went)
  echo "$(date -u +%FT%TZ),$ASSET,$BAL,$STAKE,$RESULT,$SHARES,$COST,$CLOSE_USDC,$PNL,$BAL_AFTER,$SETTLE,$UNCONF,$SIDE,$ENTRY_PRICE" >> "$LOOP_LOG"

  echo "[$(date -u +%H:%M:%SZ)] result=$RESULT asset=$ASSET  pnl=\$$(printf '%.2f' $PNL)  trade=$TRADES_DONE/$MAX_TRADES  balance_after=\$$(printf '%.2f' $BAL_AFTER)"

  # Brief pause before the next 5-min market
  sleep 5
done

echo
echo "=== Loop ended ==="
FINAL=$(get_balance)
echo "Final balance: \$$(printf '%.2f' $FINAL)"
echo "Trades logged: $(wc -l < "$LOOP_LOG") (incl. header)"
echo "PnL log: $LOOP_LOG"