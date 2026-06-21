#!/usr/bin/env python3
"""Tail btc-5m-live trade outcomes in color.

Grey  -> ENTERED  $amount  SIDE @price  slug
Green -> WON      +$value
Red   -> LOST     -$value

Reads JSON run reports from skills/btc-5m-live/runtime/btc5m_*.log.
Each run report has: opened.{cost_usdc,side,entry_price,market_slug},
closed.{close_usdc,close_success}, realized_cashflow_pnl_usdc.

Usage:
  tail_trades.py            # print all completed trades once
  tail_trades.py --follow   # keep watching for new completed runs
  tail_trades.py --follow --interval 3
"""
import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

GREY = "\033[90m"
GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"

RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"


def fmt_usd(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):.2f}"


def parse_report(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except Exception:
        return None
    if not txt.strip():
        return None
    # Report is the final JSON blob. The runner prints only the report to stdout,
    # but logs may capture stderr too — extract the last top-level JSON object.
    try:
        return json.loads(txt)
    except Exception:
        # Fall back: scan for the last balanced JSON object.
        start = txt.rfind("\n{")
        if start < 0:
            start = txt.find("{")
        if start < 0:
            return None
        for end in range(len(txt), start, -1):
            try:
                return json.loads(txt[start:end])
            except Exception:
                continue
        return None


def emit_lines(report: dict) -> None:
    opened = report.get("opened")
    if not opened:
        # No trade was entered this run — skip silently.
        return
    cost = float(opened.get("cost_usdc") or 0.0)
    side = str(opened.get("side") or "?")
    entry = float(opened.get("entry_price") or 0.0)
    slug = str(opened.get("market_slug") or "")

    # Junk filter: if there was no real cost (dust / no-fill / phantom), skip.
    if cost <= 0.01:
        return

    closed = report.get("closed") or {}
    close_usdc = float(closed.get("close_usdc") or 0.0)
    close_ok = bool(closed.get("close_success"))
    raw_pnl = report.get("realized_cashflow_pnl_usdc")

    # Source-of-truth PnL priority:
    # A. realized_cashflow_pnl_usdc if present and non-zero
    # B. close_usdc - cost_usdc
    # C. if close_usdc == 0 and cost_usdc > 0 → pnl = -cost_usdc (lost the stake)
    # D. otherwise skip (no reliable PnL)
    pnl: float | None = None
    if raw_pnl is not None:
        try:
            v = float(raw_pnl)
            if abs(v) > 0.0001 or close_usdc > 0:
                pnl = v
        except Exception:
            pass
    if pnl is None:
        if close_usdc > 0:
            pnl = close_usdc - cost
        elif cost > 0:
            # Close never happened (no fill / not redeemed) → lost the stake.
            pnl = -cost
        else:
            return

    # Clamp impossible loss: if |pnl| exceeds cost by >5% and close never
    # produced funds, the reported PnL is bogus (e.g. redeem deadline hit
    # before auto-redeem landed). Treat as lost-stake.
    if close_usdc == 0 and abs(pnl) > cost * 1.05:
        pnl = -cost

    print(f"{GREY}ENTERED  ${cost:.2f}  {side} @{entry:.4f}  {slug}{RESET}")
    sys.stdout.flush()

    if abs(pnl) < 0.01:
        print(f"{GREY}FLAT     $0.00  (closed ${close_usdc:.2f} - cost ${cost:.2f}){RESET}")
    elif pnl > 0:
        print(f"{GREEN}{BOLD}WON      {fmt_usd(pnl)}{RESET}  "
              f"{GREY}(closed ${close_usdc:.2f} - cost ${cost:.2f}){RESET}")
    else:
        print(f"{RED}{BOLD}LOST     {fmt_usd(pnl)}{RESET}  "
              f"{GREY}(closed ${close_usdc:.2f} - cost ${cost:.2f}){RESET}")
    sys.stdout.flush()


def scan_once(seen: set[str]) -> set[str]:
    files = sorted(glob.glob(str(RUNTIME_DIR / "btc5m_*.log")))
    for path in files:
        if path in seen:
            continue
        report = parse_report(path)
        if report is None:
            continue  # incomplete/empty log (run still in progress) — skip
        emit_lines(report)
        seen.add(path)
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--follow", action="store_true", help="keep watching for new runs")
    ap.add_argument("--interval", type=float, default=3.0, help="poll interval sec (follow mode)")
    args = ap.parse_args()

    if not RUNTIME_DIR.exists():
        print(f"{GREY}no runtime dir yet: {RUNTIME_DIR}{RESET}")
        return 0

    seen: set[str] = set()
    seen = scan_once(seen)
    if not args.follow:
        return 0

    try:
        while True:
            time.sleep(args.interval)
            seen = scan_once(seen)
    except KeyboardInterrupt:
        print(f"\n{GREY}stopped{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())