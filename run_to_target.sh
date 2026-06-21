#!/usr/bin/env bash
# Safe sequential loop for the sniper probability-based Polymarket 5m bot.
#
# One wallet, one live trade. No parallel batches. No progressive boosts.
# The edge improves by filtering harder, not by firing more trades.
set -euo pipefail
cd "$(dirname "$0")"
export LC_NUMERIC=C

VENV_PY="pm-hl-conservative-plus-repo/.venv/bin/python"
RUNNER="skills/btc-5m-live/scripts/test_probability_5m_session.py"
RUNTIME="skills/btc-5m-live/runtime"
ENV_FILE="pm-hl-conservative-plus-repo/.env"
LOOP_LOG="$RUNTIME/loop_pnl.csv"
mkdir -p "$RUNTIME"

ASSETS="btc"
STAKE_USD="5"
MAX_TRADES="5"
FLOOR="15"
DAILY_LOSS_PCT_LIMIT="0.10"
ENTRY_TIMEOUT_MIN="8"
POLL_SEC="2"
MIN_EDGE="0.12"
MIN_MODEL_PROB="0.78"
MIN_Z_ABS="1.10"
MIN_ENTRY_PRICE="0.22"
MAX_ENTRY_PRICE="0.70"
MAX_SPREAD="0.03"
MIN_TOP_ASK_NOTIONAL="8"
MIN_DISTANCE_PCT="0.00055"
MIN_DISTANCE_VS_SIGMA="0.45"
MIN_QUALITY_SCORE="4.0"
MIN_ENTRY_SECONDS_LEFT="55"
MAX_ENTRY_SECONDS_LEFT="135"
TAKE_PROFIT_PCT="0.25"
TAKE_PROFIT_USD="1.00"
TAKE_PROFIT_MIN_BID="0.72"
TAKE_PROFIT_MIN_SECONDS_LEFT="12"
TAKE_PROFIT_CHECK_SEC="1.5"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --assets) ASSETS="$2"; shift 2;;
    --asset) ASSETS="$2"; shift 2;;
    --stake-usd) STAKE_USD="$2"; shift 2;;
    --max-trades) MAX_TRADES="$2"; shift 2;;
    --floor) FLOOR="$2"; shift 2;;
    --daily-loss-pct) DAILY_LOSS_PCT_LIMIT="$2"; shift 2;;
    --entry-timeout-min) ENTRY_TIMEOUT_MIN="$2"; shift 2;;
    --poll-sec) POLL_SEC="$2"; shift 2;;
    --min-edge) MIN_EDGE="$2"; shift 2;;
    --min-model-prob) MIN_MODEL_PROB="$2"; shift 2;;
    --min-z-abs) MIN_Z_ABS="$2"; shift 2;;
    --min-entry-price) MIN_ENTRY_PRICE="$2"; shift 2;;
    --max-entry-price) MAX_ENTRY_PRICE="$2"; shift 2;;
    --max-spread) MAX_SPREAD="$2"; shift 2;;
    --min-top-ask-notional) MIN_TOP_ASK_NOTIONAL="$2"; shift 2;;
    --min-distance-pct) MIN_DISTANCE_PCT="$2"; shift 2;;
    --min-distance-vs-sigma) MIN_DISTANCE_VS_SIGMA="$2"; shift 2;;
    --min-quality-score) MIN_QUALITY_SCORE="$2"; shift 2;;
    --min-entry-seconds-left) MIN_ENTRY_SECONDS_LEFT="$2"; shift 2;;
    --max-entry-seconds-left) MAX_ENTRY_SECONDS_LEFT="$2"; shift 2;;
    --take-profit-pct) TAKE_PROFIT_PCT="$2"; shift 2;;
    --take-profit-usd) TAKE_PROFIT_USD="$2"; shift 2;;
    --take-profit-min-bid) TAKE_PROFIT_MIN_BID="$2"; shift 2;;
    --take-profit-min-seconds-left) TAKE_PROFIT_MIN_SECONDS_LEFT="$2"; shift 2;;
    --take-profit-check-sec) TAKE_PROFIT_CHECK_SEC="$2"; shift 2;;
    --parallel)
      echo "ERROR: --parallel disabled. One wallet-level bot may only run one live trade at a time." >&2
      exit 2
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ ! -x "$VENV_PY" ]]; then
  echo "Missing venv python: $VENV_PY" >&2
  exit 1
fi
if [[ ! -f "$RUNNER" ]]; then
  echo "Missing runner: $RUNNER" >&2
  exit 1
fi
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ ! -f "$LOOP_LOG" ]]; then
  echo "ts,asset,balance_before,stake,trade_result,shares,cost,close_usdc,pnl,balance_after,settle_wait,balance_unconfirmed,side,entry_price" > "$LOOP_LOG"
fi

get_balance() {
  "$VENV_PY" -c "
import importlib.util
from dotenv import load_dotenv
load_dotenv('$ENV_FILE')
spec = importlib.util.spec_from_file_location('r', 'skills/btc-5m-live/scripts/test_btc_5m_session_exit_sl.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
c = m.auth_clob_client()
b = m.get_pusd_balance(c)
print(f'{b:.6f}' if b is not None else '0')
" 2>/dev/null
}

parse_run() {
  local log="$1"
  "$VENV_PY" -c "
import json, sys
text = open('$log', encoding='utf-8', errors='replace').read()
start = text.find('{')
if start < 0:
    print('no_json|0|0|0|0|0|False||0'); sys.exit()
obj = None
for end in range(len(text), start, -1):
    try:
        obj = json.loads(text[start:end]); break
    except Exception:
        pass
if not obj:
    print('bad_json|0|0|0|0|0|False||0'); sys.exit()
o = obj.get('opened') or {}
c = obj.get('closed') or {}
print('|'.join(str(x) for x in [
    obj.get('result') or 'unknown',
    o.get('shares') or 0,
    o.get('cost_usdc') or 0,
    c.get('close_usdc') or 0,
    obj.get('realized_cashflow_pnl_usdc') if obj.get('realized_cashflow_pnl_usdc') is not None else 0,
    c.get('close_settle_wait_sec') or o.get('settle_wait_sec') or 0,
    o.get('balance_unconfirmed') or False,
    o.get('side') or '',
    o.get('entry_price') or 0,
]))
" 2>/dev/null
}

IFS=',' read -r -a ASSET_LIST <<< "$ASSETS"
for i in "${!ASSET_LIST[@]}"; do
  ASSET_LIST[$i]="$(echo "${ASSET_LIST[$i]}" | tr '[:upper:]' '[:lower:]' | xargs)"
done

DAY_START="$(date -u +%Y-%m-%d)"
DAY_START_BAL="$(get_balance)"
TRADES_DONE=0
ASSET_IDX=0

echo "=== SAFE sniper probability 5m loop ==="
echo "  Assets:             $ASSETS"
echo "  Max trades:         $MAX_TRADES"
echo "  Stake:              \$$STAKE_USD"
echo "  Floor:              \$$FLOOR"
echo "  Daily loss cap:     $(python3 - <<PY
print(round(float('$DAILY_LOSS_PCT_LIMIT')*100, 1))
PY
)%"
echo "  Min edge:           $MIN_EDGE"
echo "  Min model prob:     $MIN_MODEL_PROB"
echo "  Min z abs:          $MIN_Z_ABS"
echo "  Entry price band:   $MIN_ENTRY_PRICE to $MAX_ENTRY_PRICE"
echo "  Max spread:         $MAX_SPREAD"
echo "  Min depth:          \$$MIN_TOP_ASK_NOTIONAL"
echo "  Time window:        ${MIN_ENTRY_SECONDS_LEFT}s to ${MAX_ENTRY_SECONDS_LEFT}s left"
echo "  Take profit:        +\$$TAKE_PROFIT_USD or ${TAKE_PROFIT_PCT} pct, min bid $TAKE_PROFIT_MIN_BID"
echo "  Log CSV:            $LOOP_LOG"
echo "  Mode:               sequential only"
echo

echo "Day $DAY_START starting balance: \$$(printf '%.2f' "$DAY_START_BAL")"

while [[ "$TRADES_DONE" -lt "$MAX_TRADES" ]]; do
  BAL="$(get_balance)"
  if (( $(echo "$BAL < $FLOOR" | bc -l) )); then
    echo "STOP: balance \$$(printf '%.2f' "$BAL") below floor \$$FLOOR"
    break
  fi
  DAILY_LOSS="$(echo "$DAY_START_BAL - $BAL" | bc -l)"
  DAILY_LOSS_PCT="$(echo "scale=6; $DAILY_LOSS / $DAY_START_BAL" | bc -l)"
  if (( $(echo "$DAILY_LOSS_PCT >= $DAILY_LOSS_PCT_LIMIT" | bc -l) )); then
    echo "STOP: daily loss cap hit. Down $(printf '%.1f' "$(echo "$DAILY_LOSS_PCT * 100" | bc -l)")%"
    break
  fi

  ASSET="${ASSET_LIST[$ASSET_IDX]}"
  ASSET_IDX=$(( (ASSET_IDX + 1) % ${#ASSET_LIST[@]} ))
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  LOG="$RUNTIME/btc5m_sniper_${ASSET}_${TS}.log"
  echo "[$(date -u +%H:%M:%SZ)] trade $((TRADES_DONE+1))/$MAX_TRADES asset=$ASSET balance=\$$(printf '%.2f' "$BAL")"

  "$VENV_PY" "$RUNNER" \
    --asset "$ASSET" \
    --stake-usd "$STAKE_USD" \
    --entry-timeout-min "$ENTRY_TIMEOUT_MIN" \
    --poll-sec "$POLL_SEC" \
    --min-edge "$MIN_EDGE" \
    --min-model-prob "$MIN_MODEL_PROB" \
    --min-z-abs "$MIN_Z_ABS" \
    --min-entry-price "$MIN_ENTRY_PRICE" \
    --max-entry-price "$MAX_ENTRY_PRICE" \
    --max-spread "$MAX_SPREAD" \
    --min-top-ask-notional "$MIN_TOP_ASK_NOTIONAL" \
    --min-distance-pct "$MIN_DISTANCE_PCT" \
    --min-distance-vs-sigma "$MIN_DISTANCE_VS_SIGMA" \
    --min-quality-score "$MIN_QUALITY_SCORE" \
    --min-entry-seconds-left "$MIN_ENTRY_SECONDS_LEFT" \
    --max-entry-seconds-left "$MAX_ENTRY_SECONDS_LEFT" \
    --take-profit-pct "$TAKE_PROFIT_PCT" \
    --take-profit-usd "$TAKE_PROFIT_USD" \
    --take-profit-min-bid "$TAKE_PROFIT_MIN_BID" \
    --take-profit-min-seconds-left "$TAKE_PROFIT_MIN_SECONDS_LEFT" \
    --take-profit-check-sec "$TAKE_PROFIT_CHECK_SEC" \
    --execute > "$LOG" 2>&1 || true

  IFS='|' read -r RESULT SHARES COST CLOSE_USDC PNL SETTLE UNCONF SIDE ENTRY_PRICE <<< "$(parse_run "$LOG")"
  BAL_AFTER="$(get_balance)"

  if [[ "$RESULT" == "done" ]]; then
    TRADES_DONE=$((TRADES_DONE + 1))
  fi
  echo "$(date -u +%FT%TZ),$ASSET,$BAL,$STAKE_USD,$RESULT,$SHARES,$COST,$CLOSE_USDC,$PNL,$BAL_AFTER,$SETTLE,$UNCONF,$SIDE,$ENTRY_PRICE" >> "$LOOP_LOG"
  echo "[$(date -u +%H:%M:%SZ)] result=$RESULT side=$SIDE entry=$ENTRY_PRICE cost=\$$(printf '%.2f' "${COST:-0}") pnl=\$$(printf '%.2f' "${PNL:-0}") balance_after=\$$(printf '%.2f' "$BAL_AFTER") log=$LOG"
  sleep 5
done

echo
echo "=== Loop ended ==="
echo "Final balance: \$$(printf '%.2f' "$(get_balance)")"
echo "Trades done: $TRADES_DONE"
echo "CSV: $LOOP_LOG"
