#!/usr/bin/env bash
# Watcher: when combined losses in the CSV exceed $THRESHOLD, restart the bot
# to activate the tighter per-asset settings (BTC/ETH/XRP threshold=0.65, max_price=0.70).
#
# "Combined losses" = sum of absolute PnL across all losing trades in the CSV
# (cumulative since the bot started logging).
#
# Restart is graceful: we kill only `run_to_target.sh` (the loop runner). Any
# in-flight `test_btc_5m_session_exit_sl.py` python runner is left alone so its
# position settles on-chain normally — killing it mid-hold would leave the
# position stranded and likely lose the stake (we learned this the hard way).
set -uo pipefail
cd "$(dirname "$0")"
export LC_NUMERIC=C    # force period-decimal in printf/bc (avoid locale comma issues)

LOOP_LOG="skills/btc-5m-live/runtime/loop_pnl.csv"
RUNNER_LOG="skills/btc-5m-live/runtime/loop_runner.log"
THRESHOLD=55
POLL_SEC=30

echo "[$(date -u +%H:%M:%SZ)] watcher started — will restart bot when combined losses exceed \$${THRESHOLD}"

while true; do
  # Sum absolute PnL of all losing trades (pnl < 0) in the CSV
  losses=$(awk -F, 'NR>1 {
    pnl = $9 + 0
    if (pnl < 0) sum += -pnl
  } END { printf "%.2f", sum }' "$LOOP_LOG" 2>/dev/null)
  [[ -z "$losses" ]] && losses="0"

  if (( $(echo "$losses > $THRESHOLD" | bc -l 2>/dev/null || echo 0) )); then
    echo "[$(date -u +%H:%M:%SZ)] combined losses=\$${losses} exceeded \$${THRESHOLD} — restarting bot"

    # 1) Stop the loop runner (does NOT touch in-flight python runners)
    if pgrep -f "run_to_target.sh" >/dev/null 2>&1; then
      pkill -f "run_to_target.sh" 2>/dev/null || true
      echo "[$(date -u +%H:%M:%SZ)] run_to_target.sh stopped"
      sleep 2
    fi

    # 2) Wait for any in-flight python runner to finish (so we don't double-fire)
    if pgrep -f "test_btc_5m_session_exit_sl.py" >/dev/null 2>&1; then
      echo "[$(date -u +%H:%M:%SZ)] waiting for in-flight trade to settle..."
      while pgrep -f "test_btc_5m_session_exit_sl.py" >/dev/null 2>&1; do
        sleep 5
      done
      echo "[$(date -u +%H:%M:%SZ)] in-flight trade settled"
    fi

    # 3) Start the new bot with the per-asset settings active
    nohup ./run_to_target.sh --max-trades 500 > "$RUNNER_LOG" 2>&1 &
    NEW_PID=$!
    echo "[$(date -u +%H:%M:%SZ)] bot restarted pid=$NEW_PID with new per-asset settings"
    exit 0
  fi

  sleep "$POLL_SEC"
done