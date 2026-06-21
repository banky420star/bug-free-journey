#!/usr/bin/env python3
"""Probability-based 5m session runner.

Uses `probability_signal.py` for the research-backed entry decision and reuses
existing order execution helpers from `test_btc_5m_session_exit_sl.py`.

Important accounting rule:
- Wallet-level pUSD deltas are NOT safe as per-trade PnL when more than one
  trade can overlap.
- For a long binary YES/NO token, max loss is the entry cost and payoff is at
  most roughly one pUSD per share.
- This runner records per-trade PnL from bounded token economics and clamps
  impossible values.

Extra edge control:
- While a trade is open, monitor live best bid.
- If mark-to-market PnL spikes, close early with a FAK sell instead of letting
  the spike evaporate into settlement noise.
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


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def wait_for_open_balance(client, token_id: str, before: float, errors: list, timeout: float = 10.0) -> float:
    deadline = time.time() + timeout
    bal = base.get_ctf_balance(client, token_id, errors=errors)
    bal = bal if bal is not None else before
    while bal <= before + 0.05 and time.time() < deadline:
        time.sleep(0.5)
        bal = base.get_ctf_balance(client, token_id, errors=errors)
        bal = bal if bal is not None else before
    return bal


def wait_for_balance_drop(client, token_id: str, before: float, errors: list, timeout: float = 10.0) -> tuple[float, float]:
    deadline = time.time() + timeout
    start = time.time()
    bal = base.get_ctf_balance(client, token_id, errors=errors)
    bal = bal if bal is not None else before
    while bal >= before - 0.05 and time.time() < deadline:
        time.sleep(0.5)
        bal = base.get_ctf_balance(client, token_id, errors=errors)
        bal = bal if bal is not None else before
    return bal, round(time.time() - start, 2)


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


def bounded_close_pnl(shares: float, cost: float, close_usdc: float) -> tuple[float, float]:
    shares = max(0.0, float(shares or 0.0))
    cost = max(0.0, float(cost or 0.0))
    close_usdc = clamp(max(0.0, float(close_usdc or 0.0)), 0.0, shares)
    pnl = round(close_usdc - cost, 6)
    pnl = clamp(pnl, -cost, max(0.0, shares - cost))
    return round(close_usdc, 6), round(pnl, 6)


def compute_binary_pnl(shares: float, cost: float, token_redeemed: bool) -> tuple[float, float, str]:
    close_usdc, pnl = bounded_close_pnl(shares, cost, shares if token_redeemed else 0.0)
    return close_usdc, pnl, "binary_token_settlement"


def close_early_take_profit(args: argparse.Namespace, client, opened: dict[str, Any], trigger: dict[str, Any], report: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Try to close the whole position after a mark-to-market PnL spike.

    Returns a `closed` dict if the CTF balance dropped meaningfully. Otherwise
    returns None and the caller continues holding to settlement.
    """
    shares = float(opened["shares"])
    token_id = str(opened["token_id"])
    before = base.get_ctf_balance(client, token_id, errors=report["balance_errors"])
    before = before if before is not None else float(opened.get("ctf_balance_after_open") or shares)

    out, objs = base.run_close(
        args.repo,
        opened["market_slug"],
        token_id,
        shares,
        args.execute,
        close_order_type="FAK",
    )
    post = {}
    close_obj = {}
    for obj in objs:
        if isinstance(obj, dict) and "order_post_result" in obj:
            close_obj = obj
            post = obj.get("order_post_result") or {}

    remaining, settle_wait = wait_for_balance_drop(client, token_id, before, report["balance_errors"])
    closed_shares = max(0.0, round(before - remaining, 6))
    post_status = str(post.get("status") or "").lower()
    matched = post.get("success") is True and post_status == "matched"

    if closed_shares <= 0.05 and not matched:
        report.setdefault("take_profit_debug", []).append({
            "ts": ts_utc(),
            "status": "take_profit_close_not_filled",
            "trigger": trigger,
            "post": post,
            "remaining": remaining,
            "raw": out[-2000:],
        })
        return None

    close_usdc_req = safe_float(post.get("takingAmount"), 0.0)
    trigger_bid = safe_price(trigger.get("bid"), fallback=float(opened["entry_price"]))
    estimated_close_usdc = closed_shares * trigger_bid
    if close_usdc_req <= 0 or close_usdc_req > shares * 1.02:
        close_usdc_req = estimated_close_usdc

    # If only a partial FAK close landed, cost basis is still the full trade if
    # the remaining dust is negligible; otherwise report partial and continue is
    # safer. Here we only finish early when practically all shares are gone.
    fully_closed = remaining < 0.05
    if not fully_closed:
        report.setdefault("take_profit_debug", []).append({
            "ts": ts_utc(),
            "status": "take_profit_partial_close_continue_to_settlement",
            "closed_shares": closed_shares,
            "remaining": remaining,
            "trigger": trigger,
            "post": post,
            "raw": out[-2000:],
        })
        return None

    close_usdc, pnl = bounded_close_pnl(opened["shares"], opened["cost_usdc"], close_usdc_req)
    return {
        "close_reason": "take_profit_spike",
        "closed_at": ts_utc(),
        "close_success": True,
        "close_status": post.get("status") or "matched_or_balance_closed",
        "close_order_id": post.get("orderID"),
        "close_tx": (post.get("transactionsHashes") or [None])[0],
        "close_usdc": close_usdc,
        "close_shares": closed_shares,
        "ctf_remaining": remaining,
        "position_closed_on_chain": True,
        "close_settle_wait_sec": settle_wait,
        "pnl_source": "take_profit_fak_close_bounded",
        "take_profit_trigger": trigger,
        "close_raw": out[-4000:],
        "realized_pnl": pnl,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=base.default_repo_path())
    ap.add_argument("--profile", default="conservative", help="accepted for btc5m_ctl compatibility; not used")
    ap.add_argument("--asset", default="btc", choices=sorted(ps.BINANCE_SYMBOLS.keys()))
    ap.add_argument("--stake-usd", type=float, default=5.0)
    ap.add_argument("--entry-timeout-min", type=float, default=5.0)
    ap.add_argument("--poll-sec", type=float, default=2.0)
    ap.add_argument("--min-edge", type=float, default=0.12)
    ap.add_argument("--min-model-prob", type=float, default=0.78)
    ap.add_argument("--min-z-abs", type=float, default=1.10)
    ap.add_argument("--min-entry-price", type=float, default=0.22)
    ap.add_argument("--max-entry-price", type=float, default=0.70)
    ap.add_argument("--max-spread", type=float, default=0.03)
    ap.add_argument("--min-top-ask-notional", type=float, default=8.0)
    ap.add_argument("--min-distance-pct", type=float, default=0.00055)
    ap.add_argument("--min-distance-vs-sigma", type=float, default=0.45)
    ap.add_argument("--min-quality-score", type=float, default=4.0)
    ap.add_argument("--min-entry-seconds-left", type=float, default=55.0)
    ap.add_argument("--max-entry-seconds-left", type=float, default=135.0)
    ap.add_argument("--take-profit-pct", type=float, default=0.25, help="close early if mark PnL / cost >= this")
    ap.add_argument("--take-profit-usd", type=float, default=1.00, help="close early if mark PnL >= this")
    ap.add_argument("--take-profit-min-bid", type=float, default=0.72, help="do not early-close below this bid")
    ap.add_argument("--take-profit-min-seconds-left", type=float, default=12.0, help="avoid TP attempts in final seconds")
    ap.add_argument("--take-profit-check-sec", type=float, default=1.5)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    cfg = ps.SignalConfig(
        min_edge=args.min_edge,
        min_model_prob=args.min_model_prob,
        min_z_abs=args.min_z_abs,
        min_entry_price=args.min_entry_price,
        max_entry_price=args.max_entry_price,
        max_spread=args.max_spread,
        min_top_ask_notional=args.min_top_ask_notional,
        min_distance_pct=args.min_distance_pct,
        min_distance_vs_sigma=args.min_distance_vs_sigma,
        min_seconds_left=args.min_entry_seconds_left,
        max_seconds_left=args.max_entry_seconds_left,
        min_quality_score=args.min_quality_score,
    )

    report: dict[str, Any] = {
        "started_at": ts_utc(),
        "strategy": "probability_sniper_v2_take_profit_safe_pnl",
        "params": vars(args),
        "attempts": [],
        "balance_errors": [],
        "mark_history": [],
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

            fill_price = safe_price(runner.get("entry_price"), fallback=ask)
            cost = round(shares * fill_price, 6)
            requested_cost = safe_float(post.get("makingAmount"), 0.0)
            if 0 < requested_cost <= max(stake * 1.20, cost * 1.20):
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

    # Monitor for a profit spike. If bid jumps enough, sell early and bank it.
    while time.time() < end_ts:
        seconds_left = max(0.0, end_ts - time.time())
        try:
            bid = base.clob_best_bid(opened["token_id"])
        except Exception as exc:
            report.setdefault("mark_errors", []).append({"ts": ts_utc(), "error": f"{type(exc).__name__}: {exc}"})
            bid = None

        if bid is not None and bid > 0:
            bid = safe_price(bid, fallback=opened["entry_price"])
            mark_usdc = round(opened["shares"] * bid, 6)
            mark_pnl = round(mark_usdc - opened["cost_usdc"], 6)
            mark_pct = mark_pnl / opened["cost_usdc"] if opened["cost_usdc"] > 0 else 0.0
            mark = {
                "ts": ts_utc(),
                "bid": bid,
                "mark_usdc": mark_usdc,
                "mark_pnl": mark_pnl,
                "mark_pct": round(mark_pct, 6),
                "seconds_left": seconds_left,
            }
            report["mark_history"].append(mark)
            if len(report["mark_history"]) > 25:
                report["mark_history"] = report["mark_history"][-25:]

            hit_tp = (
                seconds_left >= args.take_profit_min_seconds_left
                and bid >= args.take_profit_min_bid
                and (mark_pnl >= args.take_profit_usd or mark_pct >= args.take_profit_pct)
            )
            if hit_tp:
                closed = close_early_take_profit(args, client, opened, mark, report)
                if closed is not None:
                    report["closed"] = {k: v for k, v in closed.items() if k != "realized_pnl"}
                    report["realized_cashflow_pnl_usdc"] = closed["realized_pnl"]
                    report["finished_at"] = ts_utc()
                    report["result"] = "done"
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return 0

        time.sleep(max(0.5, float(args.take_profit_check_sec)))

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
