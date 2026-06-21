#!/usr/bin/env bash
# Full live pipeline for the BTC 5m Polymarket skill.
# Stops any existing run, then starts a fresh live trade via btc5m_ctl.sh.
# Logs go to skills/btc-5m-live/runtime/btc5m_<profile>_<ts>.log (picked up by tail_trades.py).
#
# Usage:
#   ./run_live.sh                                # conservative, 35-min entry timeout, $5 stake
#   ./run_live.sh --threshold 0.62 --entry-timeout-min 30
#   ./run_live.sh --profile aggressive --stake-usd 10
#
# ⚠️ This posts REAL orders with REAL pUSD. Only run after rotating creds + funding.
set -euo pipefail
cd "$(dirname "$0")"

CTL="skills/btc-5m-live/scripts/btc5m_ctl.sh"

if "$CTL" status | grep -q "running"; then
  echo "stopping existing run..."
  "$CTL" stop || true
fi

exec "$CTL" start "$@"