#!/usr/bin/env python3
"""Probability-based 5m session runner.

Uses `probability_signal.py` for the research-backed entry decision and reuses
existing order execution helpers from `test_btc_5m_session_exit_sl.py`.

Important accounting rule:
- Wallet-level pUSD deltas are NOT safe as per-trade PnL when more than one
  trade can overlap.
- For a long binary YES/NO token held to settlement, max loss is the entry cost
  and payoff is approximately `shares` when the token wins, otherwise 0.
- This runner therefore records per-trade PnL from token settlement state:
      pnl = (shares if redeemed/won else 0) - cost
  and clamps impossible values.
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


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def safe_price(v: Any, fallback: float) -> float:
    try:
        x = float(v)
    except Exception:
        x = fallback
    if not (0.01 <= x <= 0.99):
        x = fallback
    return clamp(float(x), 0.01, 0.99)


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


def compute_binary_pnl(shares: float, cost: float, token_redeemed: bool) -> tuple[float, float, str]:
    """Return (close_usdc, pnl, source) for a long binary token.

    If the winning token redeems, payoff is one pUSD per share. If it does not
    redeem, payoff is zero. This avoids misattributing other active trades'
    pUSD movement to this trade.
    """
    shares = max(0.0, float(shares or 0.0))
    cost = max(0.0, float(cost or 0.0))
    close_usdc = round(shares if token_redeemed else 0.0, 6)
    pnl = round(close_usdc - cost, 6)
    # Long binary safety bounds. A $5 long cannot lose $25.
    min_pnl = -cost
    max_pnl = max(0.0, shares - cost)
    pnl = clamp(pnl, min_pnl, max_pnl)
    return close_usdc, round(pnl, 6), "binary_token_settlement"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=base.default_repo_path())
    ap.add_argument("--profile", default="conservative", help="accepted for btc5m_ctl compatibility; not used")
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
        "strategy": "probability_distance_to_strike_v1_safe_pnl",
        "params": vars(args),
        "attempts": [],
        "balance_errors": [],
    }

    client = base.auth_clob_client()
    opened: Optional[dict[str, Any]] = None
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
            ask = safe_price(decision.chosen_ask, fallback=0.5)
            stake = max(float(args.stake_usd), ask * 5.0)

            ctf_before = base.get_ctf_balance(client, token_id, errors=report["balance_errors"]) or 0.0

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
            shares = max(0.0, round(ctf_after - ctf_before, 6))
            if shares <= 0.01:
                report["attempts"].append({"ts": ts_utc(), "slug": slug, "status": "open_failed_or_no_fill", "ctf_after": ctf_after})
                time.sleep(args.poll_sec)
                continue

            # Do NOT use whole-wallet pUSD delta for cost here. With overlapping
            # trades it is contaminated. Use actual CTF shares times bounded fill
            # price. This keeps entry_price within 0..1 and loss bounded by cost.
            fill_price = safe_price(runner.get("entry_price"), fallback=ask)
            cost = round(shares * fill_price, 6)
            requested_cost = 0.0
            try:
                requested_cost = float(post.get("makingAmount") or 0.0)
            except Exception:
                requested_cost = 0.0
            if 0 < requested_cost <= max(stake * 1.20, cost * 1.20):
                # If the SDK-reported making amount is sane, keep it. Otherwise
                # trust token shares × bounded fill price.
                cost = round(requested_cost, 6)
                if shares > 0:
                    fill_price = safe_price(cost / shares, fallback=fill_price)
                    cost = round(shares * fill_price, 6)

            opened = {
                "opened_at": ts_utc(),
                "market_slug": slug,
                "market_end_iso": end_iso,
                "side": side,
                "token_id": token_id,
                "entry_price": fill_price,
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
    token_redeemed = remaining < 0.05
    close_usdc, pnl, pnl_source = compute_binary_pnl(opened["shares"], opened["cost_usdc"], token_redeemed)

    report["closed"] = {
        "close_reason": "held_to_settlement",
        "closed_at": ts_utc(),
        "close_success": token_redeemed,
        "close_status": "redeemed" if token_redeemed else "worthless",
        "close_usdc": close_usdc,
        "ctf_remaining": remaining,
        "pusd_balance_after_close": pusd_after_close,
        "position_closed_on_chain": token_redeemed,
        "close_settle_wait_sec": wait_sec,
        "pnl_source": pnl_source,
    }
    report["realized_cashflow_pnl_usdc"] = pnl
    report["finished_at"] = ts_utc()
    report["result"] = "done"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
