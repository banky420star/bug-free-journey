#!/usr/bin/env bash
# Watch the live BTC 5m run: colorized tail of completed trades + raw tail of the
# current run's log. Open this in its own terminal window.
#
# Usage:
#   ./watch_live.sh                # one-shot colorized view + follow current log
#   ./watch_live.sh --follow       # keep watching for new trades
set -euo pipefail
cd "$(dirname "$0")"

VENV_PY="pm-hl-conservative-plus-repo/.venv/bin/python"
RUNTIME="skills/btc-5m-live/runtime"
TAIL="$VENV_PY skills/btc-5m-live/scripts/tail_trades.py"

follow=""
[[ "${1:-}" == "--follow" ]] && follow="--follow"

# 1) Print any completed trades so far (colorized).
"$VENV_PY" skills/btc-5m-live/scripts/tail_trades.py

# 2) Show the latest live log path.
latest=$(ls -t "$RUNTIME"/btc5m_*.log "$RUNTIME"/live_test_*.log 2>/dev/null | head -1 || true)
if [[ -n "$latest" ]]; then
  echo ""
  echo "── latest log: $latest ──"
  if [[ -n "$follow" ]]; then
    # Tail the live log raw, and also keep watching for new completed trades.
    "$VENV_PY" skills/btc-5m-live/scripts/tail_trades.py --follow --interval 3 &
    tail_pid=$!
    trap 'kill $tail_pid 2>/dev/null || true' EXIT
    tail -f "$latest"
  else
    tail -n 50 "$latest"
  fi
else
  echo "no live log yet"
fi