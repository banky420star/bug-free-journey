#!/usr/bin/env bash
# Double-click launcher: starts BTC 5m Polymarket bot backend + TUI in separate Terminal windows.
#   Window 1: TUI (curses interface with popups)
#   Window 2: backend log tail (live loop_runner.log)
#
# Current settings baked in:
#   - run_to_target.sh --max-trades 500  (sequential, 1 asset per 5-min cycle)
#   - Conservative band: threshold=0.62, max-entry-price=0.75
#   - Per-asset progressive boost (every 3 consecutive wins of same asset)
#   - Daily-loss breaker: 40%
#
# To switch to PARALLEL mode (5 assets per cycle, requires balance >= $100),
#   edit the BACKEND_CMD below to add: --parallel --parallel-min-bal 100
#
# To stop: double-click STOP_BOT.command

set -e
cd "$(dirname "$0")"

VENV_PY="pm-hl-conservative-plus-repo/.venv/bin/python"
RUNTIME_LOG="skills/btc-5m-live/runtime/loop_runner.log"
mkdir -p skills/btc-5m-live/runtime

# Stop any existing backend first (clean restart)
if pgrep -f "run_to_target.sh" >/dev/null 2>&1; then
  echo "Stopping existing backend..."
  pkill -f "run_to_target.sh" 2>/dev/null || true
  pkill -f "test_btc_5m_session_exit_sl.py" 2>/dev/null || true
  sleep 2
fi

# Start backend in background
BACKEND_CMD="./run_to_target.sh --max-trades 500"
echo "Starting backend: $BACKEND_CMD"
nohup $BACKEND_CMD > "$RUNTIME_LOG" 2>&1 &
BACKEND_PID=$!
echo "Backend pid=$BACKEND_PID, log=$RUNTIME_LOG"

# Open two Terminal windows via AppleScript
osascript <<APPLESCRIPT
tell application "Terminal"
  activate
  -- Window 1: TUI (curses display)
  do script "cd /Volumes/AI_DRIVE/polly && exec pm-hl-conservative-plus-repo/.venv/bin/python tui.py; echo 'TUI exited. Close this window.'"
  set name of front window to "BTC5m TUI"

  -- Window 2: backend log tail
  do script "cd /Volumes/AI_DRIVE/polly && tail -n 50 -f skills/btc-5m-live/runtime/loop_runner.log"
  set name of front window to "BTC5m Backend"
end tell
APPLESCRIPT

echo
echo "✅ Bot started. Two Terminal windows opened:"
echo "   • BTC5m TUI      — live monitor with popups"
echo "   • BTC5m Backend  — raw loop_runner.log tail"
echo
echo "To stop: double-click STOP_BOT.command"
echo "This launcher window will close in 3 seconds..."
sleep 3
exit 0