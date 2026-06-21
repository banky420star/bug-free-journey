#!/usr/bin/env python3
"""Polymarket CLOB V2 trade runner for the btc-5m-live skill.

Two modes, selected by arguments:

OPEN  --market-slug <slug> --force-side UP|DOWN --start-equity <n>
        --risk-frac <f> --max-notional-usd <usd> [--execute]
        -> BUY the stronger side at best ask, capped by --max-notional-usd.

CLOSE --market-slug <slug> --close-token-id <token> --close-shares <n>
        [--close-limit-price <p>] [--execute]
        -> SELL the given shares at best bid (or limit price).

Uses py-clob-client-v2 (CLOB V2 / pUSD collateral). V1 (py-clob-client) is
broken since Polymarket's Apr 2026 V2 cutover ("invalid order version").

Env (loaded from .env via BTC5M_ENV_FILE or repo/.env):
  PM_PRIVATE_KEY      EVM signing key (signer for the Polymarket proxy)
  PM_FUNDER           Polymarket proxy wallet address holding pUSD
  PM_SIGNATURE_TYPE   V2 numbering: 1=POLY_PROXY (default), 2=POLY_GNOSIS_SAFE,
                      0=EOA, 3=POLY_1271
  PM_API_KEY/SECRET/PASSPHRASE  L2 creds; blank -> auto-derive via create_or_derive_api_key
  PM_BUILDER_CODE     builder code for order attribution (optional)
  PM_ORDER_TYPE       open order type: FAK|FOK|GTC|GTD (default FAK)
  PM_CLOSE_ORDER_TYPE close order type (default FAK)
  PM_MAX_SPREAD       skip open if (ask-bid)/mid > this (default 1 = off)
  PM_MIN_TOP_ASK_NOTIONAL_USD  skip open if top ask notional < this (default 0)

Output: emits JSON objects on stdout consumed by test_btc_5m_session_exit_sl.py.
Without --execute no order is posted; the emitted order_post_result has
success=false and status="dry_run".
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from py_clob_client_v2 import (
    ApiCreds,
    ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
POLYGON_CHAIN_ID = 137
ZERO_BUILDER = "0x0000000000000000000000000000000000000000000000000000000000000000"


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def load_env() -> dict[str, str]:
    env_file = os.environ.get("BTC5M_ENV_FILE")
    if env_file:
        load_dotenv(env_file, override=False)
    else:
        repo_env = Path(__file__).resolve().parents[2] / ".env"
        if repo_env.exists():
            load_dotenv(repo_env, override=False)
    return {k: (os.environ.get(k) or "") for k in (
        "PM_PRIVATE_KEY", "PM_FUNDER", "PM_ADDRESS", "PM_SIGNATURE_TYPE",
        "PM_API_KEY", "PM_API_SECRET", "PM_API_PASSPHRASE",
        "PM_ORDER_TYPE", "PM_CLOSE_ORDER_TYPE",
        "PM_MAX_SPREAD", "PM_MIN_TOP_ASK_NOTIONAL_USD",
        "PM_BUILDER_CODE",
    )}


def auth_client(env: dict[str, str]) -> Optional[ClobClient]:
    key = env["PM_PRIVATE_KEY"]
    funder = env["PM_FUNDER"] or env["PM_ADDRESS"]
    sig = int(env["PM_SIGNATURE_TYPE"] or "1")
    api_key = env.get("PM_API_KEY", "")
    api_secret = env.get("PM_API_SECRET", "")
    api_pass = env.get("PM_API_PASSPHRASE", "")
    if not key:
        return None
    try:
        c = ClobClient(
            host=CLOB_HOST,
            chain_id=POLYGON_CHAIN_ID,
            key=key,
            signature_type=sig,
            funder=funder or None,
        )
        if api_key and api_secret and api_pass:
            c.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass))
        else:
            creds = c.create_or_derive_api_key()
            if creds is None or not getattr(creds, "api_key", None):
                return None
            c.set_api_creds(creds)
        return c
    except Exception as e:
        sys.stderr.write(f"auth_client_failed: {e}\n")
        return None


def fetch_event(slug: str) -> Optional[dict[str, Any]]:
    r = requests.get(GAMMA_EVENTS, params={"slug": slug}, timeout=12)
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None


def market_tokens(market: dict[str, Any]) -> tuple[list[str], list[float]]:
    def parse(x: Any) -> list[Any]:
        if isinstance(x, list):
            return x
        if isinstance(x, str):
            try:
                return json.loads(x)
            except Exception:
                return []
        return []
    return parse(market.get("clobTokenIds")), [float(p) for p in parse(market.get("outcomePrices"))]


def best_bid_ask(client: ClobClient, token_id: str) -> tuple[float, float, float, float, float]:
    """Return (best_bid, best_ask, top_ask_notional_usd, min_order_size, tick_size)."""
    book = client.get_order_book(str(token_id))
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bb = float(bids[0]["price"]) if bids else 0.0
    ba = float(asks[0]["price"]) if asks else 0.0
    top_ask_sz = float(asks[0]["size"]) if asks else 0.0
    top_ask_notional = top_ask_sz * ba if ba > 0 else 0.0
    min_size = float(book.get("min_order_size") or 0)
    tick = float(book.get("tick_size") or 0.01)
    return bb, ba, top_ask_notional, min_size, tick


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 6)


def size_step_for_maker_precision(price: float, tick: float) -> float:
    """Smallest `size` increment that keeps `size * price` within 2 decimals
    (Polymarket V2 maker-amount precision cap). taker (size) itself can have
    up to 4 decimals.

    For tick=0.01 (price 2 decimals), size must be integer → step=1.
    For tick=0.001 (price 3 decimals), size must be multiple of 10 → step=10.
    For tick=0.0001, step=100. Below tick=0.01 we still round size to 0.01 to
    keep taker ≤4 decimals.
    """
    if tick >= 0.01:
        return 1.0
    if tick >= 0.001:
        return 10.0
    if tick >= 0.0001:
        return 100.0
    return 1000.0


def round_size_for_maker(size: float, price: float, tick: float) -> float:
    """Floor `size` to the nearest step that keeps maker (size*price) ≤ 2 decimals."""
    step = size_step_for_maker_precision(price, tick)
    return max(0.0, (size // step) * step)


def order_type_from(name: str, default: str = "FAK") -> OrderType:
    n = (name or default).upper()
    return {
        "FAK": OrderType.FAK,
        "FOK": OrderType.FOK,
        "GTC": OrderType.GTC,
        "GTD": OrderType.GTD,
    }.get(n, OrderType.FAK)


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-slug", required=True)
    ap.add_argument("--force-side", choices=["UP", "DOWN", "YES", "NO"])
    ap.add_argument("--start-equity", type=float, default=100.0)
    ap.add_argument("--risk-frac", type=float, default=0.05)
    ap.add_argument("--max-notional-usd", type=float, default=5.0)
    ap.add_argument("--close-token-id", default=None)
    ap.add_argument("--close-shares", type=float, default=None)
    ap.add_argument("--close-limit-price", type=float, default=None)
    ap.add_argument("--execute", action="store_true")
    return ap.parse_args(argv)


def run_open(args: argparse.Namespace, env: dict[str, str]) -> int:
    side = (args.force_side or "UP").upper()
    if side == "YES":
        side = "UP"
    if side == "NO":
        side = "DOWN"

    ev = fetch_event(args.market_slug)
    if not ev:
        emit({"order_post_result": {"success": False, "status": "error", "errorMsg": "event_not_found"}, "market_slug": args.market_slug})
        return 1
    mkts = ev.get("markets") or []
    if not mkts:
        emit({"order_post_result": {"success": False, "status": "error", "errorMsg": "no_markets"}, "market_slug": args.market_slug})
        return 1
    tokens, _prices = market_tokens(mkts[0])
    if len(tokens) < 2:
        emit({"order_post_result": {"success": False, "status": "error", "errorMsg": "no_tokens"}, "market_slug": args.market_slug})
        return 1
    token_id = tokens[0] if side == "UP" else tokens[1]

    client = auth_client(env)
    if client is None:
        emit({"order_post_result": {"success": False, "status": "error", "errorMsg": "missing_credentials"}, "market_slug": args.market_slug, "token_id": token_id})
        return 2

    try:
        bb, ba, top_notional, min_size, tick = best_bid_ask(client, token_id)
    except Exception as e:
        emit({"order_post_result": {"success": False, "status": "error", "errorMsg": f"book_fetch_failed: {e}"}, "market_slug": args.market_slug, "token_id": token_id})
        return 1

    if ba <= 0:
        emit({"order_post_result": {"success": False, "status": "error", "errorMsg": "no_ask"}, "market_slug": args.market_slug, "token_id": token_id})
        return 1

    max_spread = float(env.get("PM_MAX_SPREAD") or "1")
    min_notional = float(env.get("PM_MIN_TOP_ASK_NOTIONAL_USD") or "0")
    mid = (bb + ba) / 2 if bb > 0 else ba
    spread = (ba - bb) / mid if mid > 0 else 1.0
    if max_spread < 1 and spread > max_spread:
        emit({"order_post_result": {"success": False, "status": "skip_spread", "errorMsg": f"spread {spread:.3f} > {max_spread}"}, "market_slug": args.market_slug, "token_id": token_id, "best_ask": ba, "best_bid": bb})
        return 0
    if min_notional > 0 and top_notional < min_notional:
        emit({"order_post_result": {"success": False, "status": "skip_liquidity", "errorMsg": f"top ask notional {top_notional:.2f} < {min_notional}"}, "market_slug": args.market_slug, "token_id": token_id})
        return 0

    try:
        neg_risk = bool(client.get_neg_risk(str(token_id)))
    except Exception:
        neg_risk = False

    price = round_to_tick(ba, tick)
    budget = max(0.01, float(args.max_notional_usd))
    raw_size = budget / price
    size = round_size_for_maker(raw_size, price, tick)

    if min_size > 0 and size < min_size:
        # Floor rounding dropped us below min_order_size; bump to min_size if
        # that still keeps maker (size*price) within 2 decimals (it does for
        # integer min_size and tick>=0.01).
        if min_size == int(min_size) and tick >= 0.01:
            size = float(int(min_size))
        else:
            emit({"order_post_result": {"success": False, "status": "skip_size_below_min", "errorMsg": f"size {size} < min_order_size {min_size} (budget ${budget:.2f} too small for price {price})"}, "market_slug": args.market_slug, "token_id": token_id, "min_order_size": min_size})
            return 0
    if size <= 0:
        emit({"order_post_result": {"success": False, "status": "skip_size", "errorMsg": "size_zero"}, "market_slug": args.market_slug, "token_id": token_id})
        return 0

    # Final guard: verify maker amount is within 2 decimals (tolerant of float noise).
    maker = size * price
    if abs(maker - round(maker, 2)) > 1e-9:
        emit({"order_post_result": {"success": False, "status": "skip_maker_precision", "errorMsg": f"maker {maker} exceeds 2 decimals (size={size} price={price})"}, "market_slug": args.market_slug, "token_id": token_id})
        return 0

    base = {
        "market_slug": args.market_slug,
        "token_id": str(token_id),
        "side": side,
        "best_bid": bb,
        "best_ask": ba,
        "price": price,
        "size": size,
        "budget_usd": budget,
        "min_order_size": min_size,
        "neg_risk": neg_risk,
        "execute": bool(args.execute),
    }

    if not args.execute:
        emit({
            "order_post_result": {
                "success": False,
                "status": "dry_run",
                "makingAmount": f"{budget:.6f}",
                "takingAmount": f"{size:.6f}",
            },
            "entry_price": price,
            **base,
        })
        return 0

    try:
        builder_code = env.get("PM_BUILDER_CODE") or ZERO_BUILDER
        order_args = OrderArgs(token_id=str(token_id), price=price, size=size, side=Side.BUY, builder_code=builder_code)
        options = PartialCreateOrderOptions(tick_size=f"{tick:g}", neg_risk=neg_risk)
        signed = client.create_order(order_args, options)
        ot = order_type_from(env.get("PM_ORDER_TYPE"), "FAK")
        post = client.post_order(signed, ot)
        if not isinstance(post, dict):
            post = {"success": False, "status": "error", "errorMsg": f"unexpected_post_result: {post!r}"}
        entry_price = price
        try:
            taking = float(post.get("takingAmount") or 0)
            making = float(post.get("makingAmount") or 0)
            if taking > 0 and making > 0:
                entry_price = making / taking
        except Exception:
            pass
        emit({"order_post_result": post, "entry_price": entry_price, **base})
        return 0 if post.get("success") else 1
    except Exception as e:
        emit({"order_post_result": {"success": False, "status": "error", "errorMsg": str(e)}, **base})
        return 1


def run_close(args: argparse.Namespace, env: dict[str, str]) -> int:
    token_id = args.close_token_id
    shares = float(args.close_shares or 0.0)
    if not token_id or shares <= 0:
        emit({"close_skipped": "zero_effective_shares", "order_post_result": {"success": False, "status": "skipped"}, "market_slug": args.market_slug, "token_id": str(token_id)})
        return 0

    client = auth_client(env)
    if client is None:
        emit({"close_skipped": "", "order_post_result": {"success": False, "status": "error", "errorMsg": "missing_credentials"}, "market_slug": args.market_slug, "token_id": str(token_id)})
        return 2

    try:
        neg_risk = bool(client.get_neg_risk(str(token_id)))
    except Exception:
        neg_risk = False

    if args.close_limit_price is not None and args.close_limit_price > 0:
        try:
            tick = float(client.get_tick_size(str(token_id)))
        except Exception:
            tick = 0.01
        price = round_to_tick(float(args.close_limit_price), tick)
    else:
        try:
            bb, _ba, _n, _ms, tick = best_bid_ask(client, token_id)
            price = round_to_tick(bb if bb > 0 else 0.01, tick)
        except Exception as e:
            emit({"close_skipped": "", "order_post_result": {"success": False, "status": "error", "errorMsg": f"book_fetch_failed: {e}"}, "market_slug": args.market_slug, "token_id": str(token_id)})
            return 1

    # For SELL: V2 requires maker (size) ≤ 2 decimals and taker (size*price) ≤ 4 decimals.
    # The actual CTF balance has 6 decimals (e.g. 6.689188). Floor size to 2 decimals
    # so we never try to sell more than we hold AND maker stays within precision.
    # For tick ≥ 0.01 this also keeps taker (size*price) ≤ 4 decimals.
    raw_size = max(0.0, float(shares))
    if tick >= 0.01:
        size = (raw_size * 100) // 1 / 100  # floor to 2 decimals
    elif tick >= 0.001:
        size = (raw_size * 1000) // 1 / 1000  # floor to 3 (taker size*price ≤ 4 dec for 1-dec price)
    else:
        size = (raw_size * 10000) // 1 / 10000  # floor to 4 decimals
    base = {
        "market_slug": args.market_slug,
        "token_id": str(token_id),
        "side": "SELL",
        "price": price,
        "size": size,
        "neg_risk": neg_risk,
        "execute": bool(args.execute),
    }

    if not args.execute:
        emit({
            "close_skipped": "",
            "order_post_result": {
                "success": False,
                "status": "dry_run",
                "makingAmount": f"{size:.6f}",
                "takingAmount": f"{size * price:.6f}",
            },
            **base,
        })
        return 0

    try:
        builder_code = env.get("PM_BUILDER_CODE") or ZERO_BUILDER
        order_args = OrderArgs(token_id=str(token_id), price=price, size=size, side=Side.SELL, builder_code=builder_code)
        options = PartialCreateOrderOptions(tick_size=f"{tick:g}", neg_risk=neg_risk)
        signed = client.create_order(order_args, options)
        ot = order_type_from(env.get("PM_CLOSE_ORDER_TYPE"), "FAK")
        post = client.post_order(signed, ot)
        if not isinstance(post, dict):
            post = {"success": False, "status": "error", "errorMsg": f"unexpected_post_result: {post!r}"}
        emit({"close_skipped": "", "order_post_result": post, **base})
        return 0 if post.get("success") else 1
    except Exception as e:
        emit({"close_skipped": "", "order_post_result": {"success": False, "status": "error", "errorMsg": str(e)}, **base})
        return 1


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    env = load_env()
    try:
        if args.close_token_id is not None:
            return run_close(args, env)
        return run_open(args, env)
    except Exception as e:
        emit({"order_post_result": {"success": False, "status": "error", "errorMsg": f"top_level: {e}"}, "market_slug": args.market_slug})
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))