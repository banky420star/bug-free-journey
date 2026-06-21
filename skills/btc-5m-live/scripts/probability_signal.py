#!/usr/bin/env python3
"""Sniper probability signal engine for Polymarket 5m crypto up/down markets.

Research-backed design:
- public orderbook = fast signal only
- execution/reconciliation stays in the runner
- trade only when model probability clears ask by a large edge
- use distance from 5m open, time-left, realized volatility, spread/depth, and
  directional confirmation
- abstain aggressively when the signal is only average

This module is intentionally conservative. A missed trade costs $0. A weak trade
can burn the whole stake. 🧊
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
    # Edge gates
    min_edge: float = 0.12
    min_model_prob: float = 0.78
    min_z_abs: float = 1.10

    # Entry price bounds. Avoid lottery tickets and overpaying for certainty.
    min_entry_price: float = 0.22
    max_entry_price: float = 0.70

    # Book quality
    max_spread: float = 0.03
    min_top_ask_notional: float = 8.0

    # Underlying price movement
    min_distance_pct: float = 0.00055
    min_distance_vs_sigma: float = 0.45

    # Time window. Earlier = noisy. Later = oracle/restart/liquidity trap risk.
    min_seconds_left: float = 55.0
    max_seconds_left: float = 135.0

    # Probability tilt cap. Smaller than before to reduce model overconfidence.
    momentum_weight: float = 0.08

    # Score gate. A trade needs multiple conditions to line up, not just one.
    min_quality_score: float = 4.0


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
    current_high: float
    current_low: float
    current_position: float
    prev_close: float
    prev_direction: int


@dataclass
class SignalDecision:
    chosen_side: str | None
    chosen_edge: float | None
    chosen_ask: float | None
    model_prob_up: float
    model_prob_down: float
    z_abs: float
    sigma_remaining: float
    seconds_left: float
    snapshot: dict[str, Any]
    up_quote: dict[str, Any]
    down_quote: dict[str, Any]
    candidates: list[dict[str, Any]]


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def fetch_asset_snapshot(asset: str) -> AssetSnapshot:
    asset = asset.lower()
    symbol = BINANCE_SYMBOLS[asset]
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": "5m", "limit": 10},
        timeout=5,
    )
    r.raise_for_status()
    rows = r.json()
    if len(rows) < 3:
        raise RuntimeError(f"not_enough_klines_for_{symbol}")

    cur = rows[-1]
    prev = rows[-2]
    recent = rows[-8:]

    open_price = safe_float(cur[1])
    high_price = safe_float(cur[2])
    low_price = safe_float(cur[3])
    last_price = safe_float(cur[4])
    prev_open = safe_float(prev[1])
    prev_close = safe_float(prev[4])

    ranges = [abs(safe_float(x[2]) - safe_float(x[3])) for x in recent if safe_float(x[2]) and safe_float(x[3])]
    avg_range = sum(ranges) / len(ranges) if ranges else open_price * 0.001
    distance = last_price - open_price

    denom = max(1e-12, high_price - low_price)
    current_position = clamp((last_price - low_price) / denom, 0.0, 1.0) if denom else 0.5
    prev_direction = 1 if prev_close > prev_open else (-1 if prev_close < prev_open else 0)

    return AssetSnapshot(
        asset=asset,
        symbol=symbol,
        strike_proxy=open_price,
        last_price=last_price,
        distance=distance,
        move_pct=(distance / open_price) if open_price else 0.0,
        avg_5m_range=max(avg_range, open_price * 0.0005),
        current_high=high_price,
        current_low=low_price,
        current_position=current_position,
        prev_close=prev_close,
        prev_direction=prev_direction,
    )


def best_quote(token_id: str, clob_base: str = "https://clob.polymarket.com") -> Quote:
    client = ClobClient(host=clob_base, chain_id=POLYGON)
    book = client.get_order_book(str(token_id))
    bids = book.get("bids") if isinstance(book, dict) else []
    asks = book.get("asks") if isinstance(book, dict) else []
    bid = max([safe_float(x.get("price")) for x in bids] or [0.0])
    ask = min([safe_float(x.get("price")) for x in asks] or [0.0])
    top_size = 0.0
    for a in asks:
        if ask and safe_float(a.get("price")) == ask:
            top_size += safe_float(a.get("size"))
    mid = (bid + ask) / 2 if bid and ask else ask or bid
    spread = max(0.0, ask - bid) if bid and ask else 1.0
    return Quote(
        bid=bid,
        ask=ask,
        mid=mid,
        spread=spread,
        top_ask_size=top_size,
        top_ask_notional=top_size * ask if ask else 0.0,
        min_order_size=safe_float(book.get("min_order_size"), 5.0) if isinstance(book, dict) else 5.0,
        tick_size=safe_float(book.get("tick_size"), 0.01) if isinstance(book, dict) else 0.01,
    )


def estimate_probabilities(snapshot: AssetSnapshot, seconds_left: float, cfg: SignalConfig) -> tuple[float, float, float, float]:
    time_frac = clamp(seconds_left / 300.0, 0.05, 1.0)
    sigma_remaining = max(snapshot.avg_5m_range * math.sqrt(time_frac), snapshot.strike_proxy * 0.00018)
    z = snapshot.distance / sigma_remaining
    base_up = normal_cdf(z)

    # Smaller tilt than previous version. This prevents one candle flicker from
    # creating fantasy 90% probabilities.
    momentum_tilt = clamp(snapshot.move_pct * 160.0, -cfg.momentum_weight, cfg.momentum_weight)
    prob_up = clamp(base_up + momentum_tilt, 0.02, 0.98)
    return prob_up, 1.0 - prob_up, z, sigma_remaining


def side_direction_ok(side: str, snapshot: AssetSnapshot) -> bool:
    if side == "UP":
        return snapshot.distance > 0 and snapshot.current_position >= 0.58
    return snapshot.distance < 0 and snapshot.current_position <= 0.42


def compute_quality_score(side: str, quote: Quote, model_prob: float, edge: float, z_abs: float, distance_vs_sigma: float, snapshot: AssetSnapshot, cfg: SignalConfig) -> float:
    score = 0.0
    if model_prob >= cfg.min_model_prob:
        score += 1.0
    if edge >= cfg.min_edge:
        score += 1.0
    if z_abs >= cfg.min_z_abs:
        score += 1.0
    if distance_vs_sigma >= cfg.min_distance_vs_sigma:
        score += 1.0
    if quote.spread <= cfg.max_spread * 0.67:
        score += 0.5
    if quote.top_ask_notional >= cfg.min_top_ask_notional * 2:
        score += 0.5
    # Mild continuation confirmation from prior candle, not required alone.
    if (side == "UP" and snapshot.prev_direction >= 0) or (side == "DOWN" and snapshot.prev_direction <= 0):
        score += 0.5
    return round(score, 3)


def evaluate_side(
    side: str,
    quote: Quote,
    model_prob: float,
    seconds_left: float,
    distance_pct: float,
    distance_vs_sigma: float,
    z_abs: float,
    snapshot: AssetSnapshot,
    cfg: SignalConfig,
) -> dict[str, Any]:
    reasons: list[str] = []
    if seconds_left < cfg.min_seconds_left:
        reasons.append("too_late")
    if seconds_left > cfg.max_seconds_left:
        reasons.append("too_early")
    if distance_pct < cfg.min_distance_pct:
        reasons.append("distance_too_small")
    if distance_vs_sigma < cfg.min_distance_vs_sigma:
        reasons.append("distance_vs_sigma_too_small")
    if z_abs < cfg.min_z_abs:
        reasons.append("z_too_small")
    if not side_direction_ok(side, snapshot):
        reasons.append("direction_not_confirmed")
    if quote.ask <= 0:
        reasons.append("no_ask")
    if quote.ask < cfg.min_entry_price:
        reasons.append("ask_below_min_lottery_zone")
    if quote.ask > cfg.max_entry_price:
        reasons.append("ask_above_max")
    if quote.spread > cfg.max_spread:
        reasons.append("spread_too_wide")
    if quote.top_ask_notional < cfg.min_top_ask_notional:
        reasons.append("depth_too_thin")

    edge = model_prob - quote.ask
    if model_prob < cfg.min_model_prob:
        reasons.append("model_prob_too_low")
    if edge < cfg.min_edge:
        reasons.append("edge_too_small")

    quality_score = compute_quality_score(side, quote, model_prob, edge, z_abs, distance_vs_sigma, snapshot, cfg)
    if quality_score < cfg.min_quality_score:
        reasons.append("quality_score_too_low")

    return {
        "side": side,
        "ask": quote.ask,
        "bid": quote.bid,
        "spread": quote.spread,
        "model_probability": model_prob,
        "edge": edge,
        "z_abs": z_abs,
        "distance_vs_sigma": distance_vs_sigma,
        "quality_score": quality_score,
        "top_ask_notional": quote.top_ask_notional,
        "reasons": reasons,
        "tradable": not reasons,
    }


def decide(asset: str, up_token: str, down_token: str, seconds_left: float, cfg: SignalConfig | None = None) -> SignalDecision:
    cfg = cfg or SignalConfig()
    snap = fetch_asset_snapshot(asset)
    up_q = best_quote(up_token)
    down_q = best_quote(down_token)
    prob_up, prob_down, z, sigma_remaining = estimate_probabilities(snap, seconds_left, cfg)
    z_abs = abs(z)
    distance_pct = abs(snap.distance) / snap.strike_proxy if snap.strike_proxy else 0.0
    distance_vs_sigma = abs(snap.distance) / sigma_remaining if sigma_remaining else 0.0

    candidates = [
        evaluate_side("UP", up_q, prob_up, seconds_left, distance_pct, distance_vs_sigma, z_abs, snap, cfg),
        evaluate_side("DOWN", down_q, prob_down, seconds_left, distance_pct, distance_vs_sigma, z_abs, snap, cfg),
    ]
    tradable = [c for c in candidates if c["tradable"]]
    chosen = max(tradable, key=lambda c: (c["quality_score"], c["edge"])) if tradable else None
    return SignalDecision(
        chosen_side=chosen["side"] if chosen else None,
        chosen_edge=chosen["edge"] if chosen else None,
        chosen_ask=chosen["ask"] if chosen else None,
        model_prob_up=prob_up,
        model_prob_down=prob_down,
        z_abs=z_abs,
        sigma_remaining=sigma_remaining,
        seconds_left=seconds_left,
        snapshot=asdict(snap),
        up_quote=asdict(up_q),
        down_quote=asdict(down_q),
        candidates=candidates,
    )
