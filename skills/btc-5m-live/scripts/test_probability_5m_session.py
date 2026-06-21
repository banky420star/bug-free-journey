#!/usr/bin/env python3
"""Probability-based 5m session runner.

This file is intentionally thin: it uses `probability_signal.py` for the new
research-backed entry decision and reuses the existing working execution and
balance reconciliation helpers from `test_btc_5m_session_exit_sl.py`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from typing import Any, Optional

import probability_signal as ps
import test_btc_5m_session_exit_sl as base

UTC = dt.timezone.utc


def ts_utc() -> str:
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_end_ts(end_iso: str) -> float:
    return dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp()


def wait_for_open_balance(client, token_id: str, before: float, errors: list, timeout: float = 10.0) -> float:
    deadline = time.time() + timeout
    bal = base.get_ctf_balance(client, token_id, errors=errors)
    bal = bal if bal is not None else before
    while bal <= before + 0.05 and time.time() < deadline:
        time.sleep(0.5)
        bal = base.get_ctf_balance(client, token_id, errors=errors)
        bal = bal if bal is not None else before
    return bal


def wait_for_settlement(client, token_id: str, errors: list, timeout: float = 180.0) -> tuple[float, float]:
    deadline = time.time() + timeout
    start = time.time()
    remaining = base.get_ctf_balance(client, token_id, errors=errors)
    remaining = remaining if remaining is not None else 0.0
    while remaining > 0.05 and time.time() < deadline:
        time.sleep(2.0)
        remaining = base.get_ctf_balance(client, token_id, errors=errors)
        remaining = remaining if remaining is not None else 0.0
    return remaining, round(time.time() - start, 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=base.default_repo_path())
    ap.add_argument("--asset", default="btc", choices=sorted(ps.BINANCE_SYMBOLS.keys()))
    ap.add_argument("--stake-usd", type=float, default=5.0)
    ap.add_argument("--entry-timeout-min", type=float, default=5.0)
    ap.add_argument("--poll-sec", type=float, default=2.0)
    ap.add_argument("--min-edge", type=float, default=0.08)
    ap.add_argument("--max-entry-price", type=float, default=0.78)
    ap.add_argument("--max-spread", type=float, default=0.04)
    ap.add_argument("--min-top-ask-notional", type=float, default=5.0)
    ap.add_argument("--min-distance-pct", type=float, default=0.00035)
    ap.add_argument("--min-entry-seconds-left", type=float, default=45.0)
    ap.add_argument("--max-entry-seconds-left", type=float, default=165.0)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    cfg = ps.SignalConfig(
        min_edge=args.min_edge,
        max_entry_price=args.max_entry_price,
        max_spread=args.max_spread,
        min_top_ask_notional=args.min_top_ask_notional,
        min_distance_pct=args.min_distance_pct,
        min_seconds_left=args.min_entry_seconds_left,
        max_seconds_left=args.max_entry_seconds_left,
    )

    report: dict[str, Any] = {
        "started_at": ts_utc(),
        "strategy": "probability_distance_to_strike_v1",
        "params": vars(args),
        "attempts": [],
        "balance_errors": [],
    }

    client = base.auth_clob_client()
    opened: Optional[dict[str, Any]] = None
    pusd_before_open: Optional[float] = None
    deadline = time.time() + args.entry_timeout_min * 60.0

    while time.time() < deadline:
        try:
            market = base.resolve_active_current_5m_market(args.asset)
            if not market:
                report["attempts"].append({"ts": ts_utc(), "status": "heartbeat_no_current_market"})
                time.sleep(args.poll_sec)
                continue

            _g_up, _g_dn, up_t, down_t, slug, end_iso = base.market_side_prices(market)
            end_ts = parse_end_ts(end_iso)
            seconds_left = max(0.0, end_ts - time.time())
            decision = ps.decide(args.asset, up_t, down_t, seconds_left, cfg)
            decision_dict = decision.__dict__
            report["attempts"].append({"ts": ts_utc(), "slug": slug, "status": "signal", **decision_dict})

            if not decision.chosen_side:
                time.sleep(args.poll_sec)
                continue

            side = decision.chosen_side
            token_id = up_t if side == "UP" else down_t
            ask = float(decision.chosen_ask or 0.0)
            stake = max(float(args.stake_usd), ask * 5.0)

            ctf_before = base.get_ctf_balance(client, token_id, errors=report["balance_errors"]) or 0.0
            pusd_before_open = base.get_pusd_balance(client, errors=report["balance_errors"])

            out, objs = base.run_open(args.repo, slug, side, stake, args.execute)
            post = {}
            runner = {}
            for obj in objs:
                if isinstance(obj, dict) and "order_post_result" in obj:
                    runner = obj
                    post = obj.get("order_post_result") or {}
            report["open_raw"] = out[-4000:]

            if not (post.get("success") is True and str(post.get("status", "")).lower() == "matched"):
                report["attempts"].append({"ts": ts_utc(), "slug": slug, "status": "open_not_matched", "post": post})
                time.sleep(args.poll_sec)
                continue

            ctf_after = wait_for_open_balance(client, token_id, ctf_before, report["balance_errors"])
            pusd_after = base.get_pusd_balance(client, errors=report["balance_errors"])
            shares = max(0.0, ctf_after - ctf_before)
            if shares <= 0.01:
                report["attempts"].append({"ts": ts_utc(), "slug": slug, "status": "open_failed_or_no_fill", "ctf_after": ctf_after})
                time.sleep(args.poll_sec)
                continue

            cost = float(post.get("makingAmount") or 0.0)
            if pusd_before_open is not None and pusd_after is not None:
                cost = max(0.0, round(pusd_before_open - pusd_after, 6))
            entry_price = cost / shares if shares > 0 and cost > 0 else float(runner.get("entry_price") or ask)
            opened = {
                "opened_at": ts_utc(),
                "market_slug": slug,
                "market_end_iso": end_iso,
                "side": side,
                "token_id": token_id,
                "entry_price": entry_price,
                "shares": shares,
                "cost_usdc": cost,
                "pusd_balance_after_open": pusd_after,
                "ctf_balance_after_open": ctf_after,
                "signal": decision_dict,
                "open_order_id": post.get("orderID"),
                "open_tx": (post.get("transactionsHashes") or [None])[0],
            }
            break
        except Exception as exc:
            report["attempts"].append({"ts": ts_utc(), "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            time.sleep(args.poll_sec)

    if not opened:
        report["finished_at"] = ts_utc()
        report["result"] = "no_entry_timeout"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    report["opened"] = opened
    end_ts = parse_end_ts(opened["market_end_iso"])
    while time.time() < end_ts:
        time.sleep(args.poll_sec)

    remaining, wait_sec = wait_for_settlement(client, opened["token_id"], report["balance_errors"])
    pusd_after_close = base.get_pusd_balance(client, errors=report["balance_errors"])
    close_usdc = 0.0
    if opened.get("pusd_balance_after_open") is not None and pusd_after_close is not None:
        close_usdc = max(0.0, round(pusd_after_close - opened["pusd_balance_after_open"], 6))

    position_closed = remaining < 0.05
    report["closed"] = {
        "close_reason": "held_to_settlement",
        "closed_at": ts_utc(),
        "close_success": position_closed,
        "close_status": "redeemed" if position_closed else "worthless",
        "close_usdc": close_usdc,
        "ctf_remaining": remaining,
        "pusd_balance_after_close": pusd_after_close,
        "position_closed_on_chain": position_closed,
        "close_settle_wait_sec": wait_sec,
    }
    if pusd_before_open is not None and pusd_after_close is not None:
        report["realized_cashflow_pnl_usdc"] = round(pusd_after_close - pusd_before_open, 6)
    else:
        report["realized_cashflow_pnl_usdc"] = round(close_usdc - opened["cost_usdc"], 6)
    report["finished_at"] = ts_utc()
    report["result"] = "done"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
