#!/usr/bin/env bash
# Double-click launcher: stops the BTC 5m Polymarket bot backend cleanly.
# In-flight trades are left to settle on-chain (their PnL lands in the balance
# but may not be logged to the CSV — the TUI's Total PnL uses balance delta so
# it stays accurate regardless).

cd "$(dirname "$0")"

echo "Stopping BTC 5m bot..."

# Stop the loop runner
if pgrep -f "run_to_target.sh" >/dev/null 2>&1; then
  pkill -f "run_to_target.sh" 2>/dev/null || true
  echo "  ✓ loop runner stopped"
else
  echo "  • loop runner not running"
fi

# Stop the python runner (in-flight trade will settle on-chain)
sleep 1
if pgrep -f "test_btc_5m_session_exit_sl.py" >/dev/null 2>&1; then
  echo "  ⚠ killing in-flight python runner — its position will auto-redeem on-chain"
  pkill -f "test_btc_5m_session_exit_sl.py" 2>/dev/null || true
  sleep 1
fi

# Also stop via btc5m_ctl.sh (in case any ctl-managed run is still around)
skills/btc-5m-live/scripts/btc5m_ctl.sh stop >/dev/null 2>&1 || true

echo
echo "✅ Bot stopped. Any in-flight trades will settle on-chain at market end."
echo "This launcher window will close in 3 seconds..."
sleep 3
exit 0