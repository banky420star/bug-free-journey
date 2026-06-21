#!/usr/bin/env python3
"""Probability signal engine for Polymarket 5m crypto up/down markets.

This is the strategy brain from the research notes:
- public orderbook = fast signal only
- execution/reconciliation stays in existing runner
- trade only when model probability exceeds CLOB ask by a minimum edge
- use live top-of-book, spread, depth, time-left, and distance from 5m open
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any

import requests
from py_clob_client_v2 import ClobClient

POLYGON = 137
BINANCE_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
}


@dataclass
class SignalConfig:
    min_edge: float = 0.08
    max_entry_price: float = 0.78
    max_spread: float = 0.04
    min_top_ask_notional: float = 5.0
    min_distance_pct: float = 0.00035
    min_seconds_left: float = 45.0
    max_seconds_left: float = 165.0
    momentum_weight: float = 0.15


@dataclass
class Quote:
    bid: float
    ask: float
    mid: float
    spread: float
    top_ask_size: float
    top_ask_notional: float
    min_order_size: float
    tick_size: float


@dataclass
class AssetSnapshot:
    asset: str
    symbol: str
    strike_proxy: float
    last_price: float
    distance: float
    move_pct: float
    avg_5m_range: float


@dataclass
class SignalDecision:
    chosen_side: str | None
    chosen_edge: float | None
    chosen_ask: float | None
    model_prob_up: float
    model_prob_down: float
    seconds_left: float
    snapshot: dict[str, Any]
    up_quote: dict[str, Any]
    down_quote: dict[str, Any]
    candidates: list[dict[str, Any]]


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fetch_asset_snapshot(asset: str) -> AssetSnapshot:
    asset = asset.lower()
    symbol = BINANCE_SYMBOLS[asset]
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": "5m", "limit": 8},
        timeout=5,
    )
    r.raise_for_status()
    rows = r.json()
    if len(rows) < 2:
        raise RuntimeError(f"not_enough_klines_for_{symbol}")
    cur = rows[-1]
    recent = rows[-6:]
    open_price = float(cur[1])
    last_price = float(cur[4])
    ranges = [abs(float(x[2]) - float(x[3])) for x in recent]
    avg_range = sum(ranges) / len(ranges) if ranges else open_price * 0.001
    distance = last_price - open_price
    return AssetSnapshot(
        asset=asset,
        symbol=symbol,
        strike_proxy=open_price,
        last_price=last_price,
        distance=distance,
        move_pct=(distance / open_price) if open_price else 0.0,
        avg_5m_range=max(avg_range, open_price * 0.0005),
    )


def best_quote(token_id: str, clob_base: str = "https://clob.polymarket.com") -> Quote:
    client = ClobClient(host=clob_base, chain_id=POLYGON)
    book = client.get_order_book(str(token_id))
    bids = book.get("bids") if isinstance(book, dict) else []
    asks = book.get("asks") if isinstance(book, dict) else []
    bid = max([float(x.get("price", 0) or 0) for x in bids] or [0.0])
    ask = min([float(x.get("price", 0) or 0) for x in asks] or [0.0])
    top_size = 0.0
    for a in asks:
        if ask and float(a.get("price", 0) or 0) == ask:
            top_size += float(a.get("size", 0) or 0)
    mid = (bid + ask) / 2 if bid and ask else ask or bid
    spread = max(0.0, ask - bid) if bid and ask else 1.0
    return Quote(
        bid=bid,
        ask=ask,
        mid=mid,
        spread=spread,
        top_ask_size=top_size,
        top_ask_notional=top_size * ask if ask else 0.0,
        min_order_size=float(book.get("min_order_size") or 5.0) if isinstance(book, dict) else 5.0,
        tick_size=float(book.get("tick_size") or 0.01) if isinstance(book, dict) else 0.01,
    )


def estimate_probabilities(snapshot: AssetSnapshot, seconds_left: float, cfg: SignalConfig) -> tuple[float, float, float]:
    # The 5m open is used as strike proxy because these markets are up/down over
    # the current 5m slot. Keep this conservative with distance and edge gates.
    time_frac = clamp(seconds_left / 300.0, 0.05, 1.0)
    sigma_remaining = max(snapshot.avg_5m_range * math.sqrt(time_frac), snapshot.strike_proxy * 0.00015)
    z = snapshot.distance / sigma_remaining
    base_up = normal_cdf(z)
    momentum_tilt = clamp(snapshot.move_pct * 250.0, -cfg.momentum_weight, cfg.momentum_weight)
    prob_up = clamp(base_up + momentum_tilt, 0.01, 0.99)
    return prob_up, 1.0 - prob_up, z


def evaluate_side(side: str, quote: Quote, model_prob: float, seconds_left: float, distance_pct: float, cfg: SignalConfig) -> dict[str, Any]:
    reasons: list[str] = []
    if seconds_left < cfg.min_seconds_left:
        reasons.append("too_late")
    if seconds_left > cfg.max_seconds_left:
        reasons.append("too_early")
    if distance_pct < cfg.min_distance_pct:
        reasons.append("distance_too_small")
    if quote.ask <= 0:
        reasons.append("no_ask")
    if quote.ask > cfg.max_entry_price:
        reasons.append("ask_above_max")
    if quote.spread > cfg.max_spread:
        reasons.append("spread_too_wide")
    if quote.top_ask_notional < cfg.min_top_ask_notional:
        reasons.append("depth_too_thin")
    edge = model_prob - quote.ask
    if edge < cfg.min_edge:
        reasons.append("edge_too_small")
    return {
        "side": side,
        "ask": quote.ask,
        "bid": quote.bid,
        "spread": quote.spread,
        "model_probability": model_prob,
        "edge": edge,
        "top_ask_notional": quote.top_ask_notional,
        "reasons": reasons,
        "tradable": not reasons,
    }


def decide(asset: str, up_token: str, down_token: str, seconds_left: float, cfg: SignalConfig | None = None) -> SignalDecision:
    cfg = cfg or SignalConfig()
    snap = fetch_asset_snapshot(asset)
    up_q = best_quote(up_token)
    down_q = best_quote(down_token)
    prob_up, prob_down, _z = estimate_probabilities(snap, seconds_left, cfg)
    distance_pct = abs(snap.distance) / snap.strike_proxy if snap.strike_proxy else 0.0
    candidates = [
        evaluate_side("UP", up_q, prob_up, seconds_left, distance_pct, cfg),
        evaluate_side("DOWN", down_q, prob_down, seconds_left, distance_pct, cfg),
    ]
    tradable = [c for c in candidates if c["tradable"]]
    chosen = max(tradable, key=lambda c: c["edge"]) if tradable else None
    return SignalDecision(
        chosen_side=chosen["side"] if chosen else None,
        chosen_edge=chosen["edge"] if chosen else None,
        chosen_ask=chosen["ask"] if chosen else None,
        model_prob_up=prob_up,
        model_prob_down=prob_down,
        seconds_left=seconds_left,
        snapshot=asdict(snap),
        up_quote=asdict(up_q),
        down_quote=asdict(down_q),
        candidates=candidates,
    )
