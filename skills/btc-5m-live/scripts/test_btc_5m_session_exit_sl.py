#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import subprocess
import time
from typing import Any, Optional
from pathlib import Path

import requests

from py_clob_client_v2 import ClobClient, ApiCreds, OrderMarketCancelParams
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
POLYGON = 137  # Polygon PoS chain id (py-clob-client-v2 has no POLYGON constant)

UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def ts_utc() -> str:
    return now_utc().isoformat().replace('+00:00', 'Z')


def parse_json_objects(text: str) -> list[dict[str, Any]]:
    out = []
    cur = []
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
        if depth > 0:
            cur.append(ch)
        if ch == '}' and depth > 0:
            depth -= 1
            if depth == 0:
                s = ''.join(cur)
                cur = []
                try:
                    out.append(json.loads(s))
                except Exception:
                    pass
    return out


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


def fetch_event(slug: str) -> Optional[dict[str, Any]]:
    r = requests.get('https://gamma-api.polymarket.com/events', params={'slug': slug}, timeout=12)
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None


def resolve_active_current_5m_market(asset: str = 'btc') -> Optional[dict[str, Any]]:
    """Return active 5m market for the given asset (btc/eth/sol/xrp/doge) in the current slot."""
    now = int(time.time())
    cur = bucket_5m(now)
    slug = f'{asset}-updown-5m-{cur}'

    try:
        ev = fetch_event(slug)
    except Exception:
        return None
    if not ev:
        return None

    mkts = ev.get('markets') or []
    if not mkts:
        return None

    m = mkts[0]
    if m.get('closed') is True:
        return None
    if m.get('active') is False:
        return None

    end_iso = str(m.get('endDate') or m.get('endDateIso') or '')
    try:
        end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None

    sec_left = end_ts - time.time()
    if sec_left <= 5:
        return None

    mm = dict(m)
    mm['_event_slug'] = slug
    mm['_seconds_left'] = sec_left
    return mm


def parse_json_field(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def market_side_prices(market: dict[str, Any]) -> tuple[float, float, str, str, str, str]:
    outcomes = parse_json_field(market.get('outcomes')) or []
    prices = parse_json_field(market.get('outcomePrices')) or []
    token_ids = parse_json_field(market.get('clobTokenIds')) or []
    if len(prices) < 2 or len(token_ids) < 2:
        raise RuntimeError('missing outcomePrices/clobTokenIds')

    up_i, down_i = 0, 1
    labs = [str(x).lower() for x in outcomes[:2]] if isinstance(outcomes, list) else []
    if len(labs) >= 2 and ('up' in labs[1] or 'yes' in labs[1]):
        up_i, down_i = 1, 0

    up_p = float(prices[up_i])
    dn_p = float(prices[down_i])
    up_t = str(token_ids[up_i])
    dn_t = str(token_ids[down_i])
    return up_p, dn_p, up_t, dn_t, str(market.get('slug') or market.get('_event_slug') or ''), str(market.get('endDate') or market.get('endDateIso') or '')


def _best_bid_ask(book) -> tuple[Optional[float], Optional[float]]:
    # py-clob-client-v2 returns a dict with 'bids'/'asks' lists of {'price','size'}
    bids = book.get('bids') if isinstance(book, dict) else getattr(book, 'bids', []) or []
    asks = book.get('asks') if isinstance(book, dict) else getattr(book, 'asks', []) or []
    best_bid = None
    best_ask = None
    for b in bids:
        p = float((b.get('price') if isinstance(b, dict) else getattr(b, 'price', 0)) or 0)
        if best_bid is None or p > best_bid:
            best_bid = p
    for a in asks:
        p = float((a.get('price') if isinstance(a, dict) else getattr(a, 'price', 0)) or 0)
        if best_ask is None or p < best_ask:
            best_ask = p
    return best_bid, best_ask


def clob_side_prices(up_token: str, down_token: str, clob_base: str = 'https://clob.polymarket.com') -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return trigger prices from CLOB orderbooks: UP ask, DOWN ask, spread of picked side when available."""
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    up_book = pub.get_order_book(str(up_token))
    dn_book = pub.get_order_book(str(down_token))
    up_bid, up_ask = _best_bid_ask(up_book)
    dn_bid, dn_ask = _best_bid_ask(dn_book)

    picked_spread = None
    # Side picked later by max ask; keep a generic sanity spread estimate
    if up_ask is not None and up_bid is not None:
        picked_spread = max(0.0, up_ask - up_bid)
    if dn_ask is not None and dn_bid is not None:
        s = max(0.0, dn_ask - dn_bid)
        picked_spread = s if picked_spread is None else min(picked_spread, s)

    return up_ask, dn_ask, picked_spread


def clob_best_bid(token_id: str, clob_base: str = 'https://clob.polymarket.com') -> Optional[float]:
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    book = pub.get_order_book(str(token_id))
    best_bid, _ = _best_bid_ask(book)
    return best_bid


def get_recent_btc_5m_delta_usd() -> float:
    """Fetch recent 5m BTC price move in USD (for impulse confirmation per strategy docs: ~$70-100).
    Uses public Binance kline (no key needed). Falls back gracefully.
    """
    try:
        # Last 5m kline close vs open delta
        r = requests.get(
            'https://api.binance.com/api/v3/klines',
            params={'symbol': 'BTCUSDT', 'interval': '5m', 'limit': 2},
            timeout=5
        )
        r.raise_for_status()
        klines = r.json()
        if len(klines) >= 2:
            open_p = float(klines[-1][1])  # open of most recent
            close_p = float(klines[-1][4])
            return abs(close_p - open_p)
        # Fallback: 24h change / ~288 (5m slots per day) rough
        r2 = requests.get('https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT', timeout=5)
        ch = float(r2.json().get('priceChange', 0))
        return abs(ch) / 288.0
    except Exception:
        return 0.0


def auth_clob_client(clob_base: str = 'https://clob.polymarket.com') -> Optional[ClobClient]:
    # Cache the client per-process so the TUI doesn't re-construct (and risk
    # re-deriving API creds) on every refresh. Env vars are the source of truth;
    # we only derive if PM_API_KEY/SECRET/PASSPHRASE are all missing.
    cache_key = (clob_base, os.getenv('PM_PRIVATE_KEY') or '', os.getenv('PM_FUNDER') or os.getenv('PM_ADDRESS') or '')
    cached = getattr(auth_clob_client, '_cache', {}).get(cache_key)
    if cached is not None:
        return cached
    try:
        key = os.getenv('PM_PRIVATE_KEY') or ''
        funder = os.getenv('PM_FUNDER') or os.getenv('PM_ADDRESS') or None
        sig = int(os.getenv('PM_SIGNATURE_TYPE', '1'))  # V2: 1=POLY_PROXY (default for Polymarket proxies)
        v1 = os.getenv('PM_API_KEY') or ''
        v2 = os.getenv('PM_API_SECRET') or ''
        v3 = os.getenv('PM_API_PASSPHRASE') or ''
        if not key:
            return None
        c = ClobClient(host=clob_base, chain_id=POLYGON, key=key, signature_type=sig, funder=funder)
        if v1 and v2 and v3:
            c.set_api_creds(ApiCreds(api_key=v1, api_secret=v2, api_passphrase=v3))
        else:
            creds = c.create_or_derive_api_key()
            if creds is None or not getattr(creds, 'api_key', None):
                return None
            c.set_api_creds(creds)
        if not hasattr(auth_clob_client, '_cache'):
            auth_clob_client._cache = {}
        auth_clob_client._cache[cache_key] = c
        return c
    except Exception:
        return None


def get_ctf_balance(client: Optional[ClobClient], token_id: str, errors: Optional[list] = None) -> Optional[float]:
    """Actual on-chain CTF (position-token) balance for the proxy, in shares.
    Returns None if the query fails. V2 CTF uses 6 decimals. If `errors` is
    provided, query failures are appended so the report can surface them
    instead of silently falling back to SDK-reported amounts."""
    if client is None or not token_id:
        if errors is not None:
            errors.append({'ts': ts_utc(), 'query': 'ctf_balance', 'token_id': str(token_id),
                           'error': 'no_client_or_token'})
        return None
    try:
        sig = int(os.getenv('PM_SIGNATURE_TYPE', '1'))
        r = client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=str(token_id), signature_type=sig,
        ))
        bal = r.get('balance') if isinstance(r, dict) else None
        if bal is None:
            if errors is not None:
                errors.append({'ts': ts_utc(), 'query': 'ctf_balance', 'token_id': str(token_id),
                               'error': f'no_balance_field: {r!r}'})
            return None
        return float(bal) / 1_000_000.0
    except Exception as e:
        if errors is not None:
            errors.append({'ts': ts_utc(), 'query': 'ctf_balance', 'token_id': str(token_id),
                           'error': f'{type(e).__name__}: {e}'})
        return None


def get_pusd_balance(client: Optional[ClobClient], errors: Optional[list] = None) -> Optional[float]:
    """Actual on-chain pUSD collateral balance for the proxy, in USDC units.
    Returns None if the query fails. pUSD uses 6 decimals. If `errors` is
    provided, query failures are appended so the report can surface them."""
    if client is None:
        if errors is not None:
            errors.append({'ts': ts_utc(), 'query': 'pusd_balance', 'error': 'no_client'})
        return None
    try:
        sig = int(os.getenv('PM_SIGNATURE_TYPE', '1'))
        r = client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=sig,
        ))
        bal = r.get('balance') if isinstance(r, dict) else None
        if bal is None:
            if errors is not None:
                errors.append({'ts': ts_utc(), 'query': 'pusd_balance',
                               'error': f'no_balance_field: {r!r}'})
            return None
        return float(bal) / 1_000_000.0
    except Exception as e:
        if errors is not None:
            errors.append({'ts': ts_utc(), 'query': 'pusd_balance',
                           'error': f'{type(e).__name__}: {e}'})
        return None


def position_is_dust(client: Optional[ClobClient], token_id: str, threshold: float = 0.05,
                     errors: Optional[list] = None) -> bool:
    """True if the on-chain CTF balance is below `threshold` (shares).
    A FAK close may report success=False but actually fill most of the position;
    trusting the SDK flag leaves dust undetected. Default 0.05 shares — tiny
    leftovers on a fast 5-min market aren't worth repeated close attempts.
    Returns False if the balance can't be queried (don't break the loop on
    uncertain info), but the failure is logged via `errors` if provided."""
    bal = get_ctf_balance(client, token_id, errors=errors)
    if bal is None:
        return False
    return bal < threshold


def poll_order_status(client: Optional[ClobClient], order_id: str, wait_sec: float = 6.0, step_sec: float = 1.0) -> tuple[str, Optional[dict[str, Any]]]:
    if client is None or not order_id:
        return '', None
    deadline = time.time() + max(0.0, float(wait_sec))
    last = None
    while time.time() <= deadline:
        try:
            last = client.get_order(order_id)
            st = str((last or {}).get('status') or '').upper()
            if st and st not in ('LIVE', 'OPEN'):
                return st, last
        except Exception:
            pass
        time.sleep(max(0.2, float(step_sec)))
    try:
        last = client.get_order(order_id)
    except Exception:
        pass
    st = str((last or {}).get('status') or '').upper()
    return st, last


def cancel_token_orders(client: Optional[ClobClient], token_id: str) -> Optional[dict[str, Any]]:
    if client is None:
        return None
    try:
        return client.cancel_market_orders(OrderMarketCancelParams(asset_id=str(token_id)))
    except Exception as e:
        return {'error': str(e)}


def run_open(repo: str, slug: str, side: str, stake: float, execute: bool) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        '.venv/bin/python',
        'src/live/pm_live_trade_runner.py',
        '--market-slug', slug,
        '--force-side', side,
        '--start-equity', '100',
        '--risk-frac', str(stake / 100.0),
        '--max-notional-usd', str(stake),
    ]
    if execute:
        cmd.append('--execute')
    env = os.environ.copy()
    env.setdefault('PM_MAX_SPREAD', '1')
    env.setdefault('PM_MIN_TOP_ASK_NOTIONAL_USD', '0')
    env.setdefault('PM_ORDER_TYPE', 'FAK')
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    return out, parse_json_objects(out)


def run_close(
    repo: str,
    slug: str,
    token_id: str,
    shares: float,
    execute: bool,
    close_order_type: str = 'FAK',
    close_limit_price: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        '.venv/bin/python',
        'src/live/pm_live_trade_runner.py',
        '--market-slug', slug,
        '--close-token-id', token_id,
        '--close-shares', f'{shares:.8f}',
    ]
    if close_limit_price is not None and close_limit_price > 0:
        cmd += ['--close-limit-price', f'{close_limit_price:.6f}']
    if execute:
        cmd.append('--execute')
    env = os.environ.copy()
    env['PM_CLOSE_ORDER_TYPE'] = str(close_order_type or 'FAK').upper()
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    return out, parse_json_objects(out)


def get_side_price_from_slug(slug: str, side: str) -> Optional[float]:
    try:
        ev = fetch_event(slug)
        if not ev:
            return None
        mkts = ev.get('markets') or []
        if not mkts:
            return None
        up, dn, *_ = market_side_prices(mkts[0])
        return up if side == 'UP' else dn
    except Exception:
        return None


PROFILES: dict[str, dict[str, Any]] = {
    'conservative': {
        'threshold': 0.62,
        'max_entry_price': 0.75,
        'stake_usd': 5.0,
        'stop_loss_pct': 0.0,
        'exit_before_sec': 5,
        'min_entry_seconds_left': 60,
        'entry_timeout_min': 5,
        'poll_sec': 5.0,
    },
    'aggressive': {
        'threshold': 0.58,
        'max_entry_price': 0.85,
        'stake_usd': 5.0,
        'stop_loss_pct': 0.0,
        'exit_before_sec': 5,
        'min_entry_seconds_left': 60,
        'entry_timeout_min': 5,
        'poll_sec': 5.0,
    },
}


def apply_profile(args: argparse.Namespace) -> argparse.Namespace:
    prof = PROFILES.get(args.profile or 'conservative', PROFILES['conservative'])
    if args.threshold is None:
        args.threshold = float(prof['threshold'])
    if args.max_entry_price is None:
        args.max_entry_price = float(prof.get('max_entry_price', 0.85))
    if args.stake_usd is None:
        args.stake_usd = float(prof['stake_usd'])
    if args.stop_loss_pct is None:
        args.stop_loss_pct = float(prof['stop_loss_pct'])
    if args.exit_before_sec is None:
        args.exit_before_sec = int(prof['exit_before_sec'])
    if args.min_entry_seconds_left is None:
        args.min_entry_seconds_left = int(prof['min_entry_seconds_left'])
    if args.entry_timeout_min is None:
        args.entry_timeout_min = int(prof['entry_timeout_min'])
    if args.poll_sec is None:
        args.poll_sec = float(prof['poll_sec'])
    return args


def default_repo_path() -> str:
    env_repo = os.environ.get('BTC5M_REPO')
    if env_repo:
        return env_repo
    return str(Path(__file__).resolve().parents[3] / 'pm-hl-conservative-plus-repo')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default=default_repo_path())
    ap.add_argument('--profile', choices=['conservative', 'aggressive'], default='conservative')
    ap.add_argument('--asset', default='btc', choices=['btc', 'eth', 'sol', 'xrp', 'doge'],
                    help='Which 5m up/down market to trade')
    ap.add_argument('--threshold', type=float, default=None)
    ap.add_argument('--max-entry-price', type=float, default=None, help='Skip entry if ask > this (R/R guard)')
    ap.add_argument('--stake-usd', type=float, default=None)
    ap.add_argument('--stop-loss-pct', type=float, default=None, help='0.30 means -30%% from entry price')
    ap.add_argument('--exit-before-sec', type=int, default=None)
    ap.add_argument('--min-entry-seconds-left', type=int, default=None, help='Do not open if less seconds remain in current 5m slot')
    ap.add_argument('--entry-timeout-min', type=int, default=None)
    ap.add_argument('--poll-sec', type=float, default=None)
    ap.add_argument('--close-retry-max', type=int, default=18, help='Max close retries when position is not yet visible / not immediately closable')
    ap.add_argument('--close-retry-delay-sec', type=float, default=2.0, help='Delay between close retries')
    ap.add_argument('--min-shares', type=float, default=5.0, help='Min shares per order (Polymarket 5-share minimum). If stake/entry < this, bump stake up to min_shares*entry.')
    ap.add_argument('--execute', action='store_true')
    args = apply_profile(ap.parse_args())

    report: dict[str, Any] = {
        'started_at': ts_utc(),
        'params': {
            'profile': args.profile,
            'threshold': args.threshold,
            'max_entry_price': args.max_entry_price,
            'stake_usd': args.stake_usd,
            'stop_loss_pct': args.stop_loss_pct,
            'exit_before_sec': args.exit_before_sec,
            'min_entry_seconds_left': args.min_entry_seconds_left,
            'entry_timeout_min': args.entry_timeout_min,
            'poll_sec': args.poll_sec,
            'close_retry_max': args.close_retry_max,
            'close_retry_delay_sec': args.close_retry_delay_sec,
            'execute': args.execute,
        },
        'attempts': [],
        'balance_errors': [],
    }

    deadline = time.time() + args.entry_timeout_min * 60
    opened = None
    bal_client = auth_clob_client()  # for on-chain CTF/pUSD balance queries
    pusd_before_open: Optional[float] = None  # captured right before the open order below

    while time.time() < deadline:
        try:
            m = resolve_active_current_5m_market(args.asset)
            if not m:
                report['attempts'].append({'ts': ts_utc(), 'status': 'heartbeat_no_current_market'})
                time.sleep(args.poll_sec)
                continue

            g_up, g_dn, up_t, dn_t, slug, end_iso = market_side_prices(m)

            end_ts = None
            sec_left = None
            try:
                end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
                sec_left = max(0.0, end_ts - time.time())
            except Exception:
                pass

            if sec_left is None:
                report['attempts'].append({'ts': ts_utc(), 'slug': slug, 'status': 'heartbeat_bad_market_end'})
                time.sleep(args.poll_sec)
                continue

            # Do not open if less than N seconds remain in current slot.
            if sec_left < args.min_entry_seconds_left:
                report['attempts'].append({
                    'ts': ts_utc(),
                    'slug': slug,
                    'status': 'skip_too_late_to_enter',
                    'seconds_left': sec_left,
                    'min_entry_seconds_left': args.min_entry_seconds_left,
                })
                time.sleep(args.poll_sec)
                continue

            # CLOB-based trigger price (best ask of selected side), not Gamma outcomePrices.
            try:
                up_ask, dn_ask, min_spread = clob_side_prices(up_t, dn_t)
            except Exception as e:
                report['attempts'].append({'ts': ts_utc(), 'slug': slug, 'status': 'skip_clob_unavailable', 'error': str(e)})
                time.sleep(args.poll_sec)
                continue

            delta_usd = get_recent_btc_5m_delta_usd()
            report['attempts'].append({
                'ts': ts_utc(),
                'slug': slug,
                'status': 'heartbeat',
                'gamma_up': g_up,
                'gamma_down': g_dn,
                'clob_up_ask': up_ask,
                'clob_down_ask': dn_ask,
                'seconds_left': sec_left,
                'min_spread': min_spread,
                'btc_5m_delta_usd': round(delta_usd, 2),
            })

            candidates: list[tuple[str, float]] = []
            if up_ask is not None and float(up_ask) >= args.threshold and float(up_ask) <= args.max_entry_price:
                candidates.append(('UP', float(up_ask)))
            if dn_ask is not None and float(dn_ask) >= args.threshold and float(dn_ask) <= args.max_entry_price:
                candidates.append(('DOWN', float(dn_ask)))

            if not candidates:
                report['attempts'].append({
                    'ts': ts_utc(),
                    'slug': slug,
                    'status': 'skip_price_outside_band',
                    'threshold': args.threshold,
                    'max_entry_price': args.max_entry_price,
                    'clob_up_ask': up_ask,
                    'clob_down_ask': dn_ask,
                    'seconds_left': sec_left,
                })
                time.sleep(args.poll_sec)
                continue

            side, trigger_price = sorted(candidates, key=lambda x: x[1], reverse=True)[0]

            # Min-order rule: Polymarket requires >= 5 shares per order.
            # If stake / entry_price < min_shares, bump the stake up to min_shares * entry_price
            # so the order meets the minimum. If the bumped stake exceeds the pUSD balance,
            # skip this attempt (can't afford the minimum).
            stake_usd = args.stake_usd
            shares_expected = stake_usd / trigger_price if trigger_price > 0 else 0
            if shares_expected < args.min_shares:
                stake_usd = round(args.min_shares * trigger_price, 6)
                shares_expected = stake_usd / trigger_price if trigger_price > 0 else 0
                # Re-check affordability with a quick pUSD snapshot
                _pusd_check = get_pusd_balance(bal_client, errors=report.get('balance_errors'))
                if _pusd_check is not None and _pusd_check < stake_usd:
                    report['attempts'].append({
                        'ts': ts_utc(),
                        'slug': slug,
                        'status': 'below_min_order_size',
                        'stake_needed_usd': stake_usd,
                        'pusd_available': _pusd_check,
                        'min_shares': args.min_shares,
                        'trigger_price': trigger_price,
                        'seconds_left': sec_left,
                    })
                    time.sleep(args.poll_sec)
                    continue

            # Snapshot pUSD AND CTF balance RIGHT BEFORE the open order so:
            #  - cost = pusd_before - pusd_after is clean even after long polling
            #  - we can detect the new fill as an INCREASE over pre-existing dust
            #    (a prior bug waited only until balance > 0, which is true immediately
            #    if leftover dust from a previous trade is sitting on the token).
            pusd_before_open = get_pusd_balance(bal_client, errors=report.get('balance_errors'))
            token_id_pre = str(up_t if side == 'UP' else dn_t)
            ctf_before_open = get_ctf_balance(bal_client, token_id_pre, errors=report.get('balance_errors'))
            ctf_before_open = ctf_before_open if ctf_before_open is not None else 0.0

            out, objs = run_open(args.repo, slug, side, stake_usd, args.execute)
            post = None
            runner = None
            for o in objs:
                if isinstance(o, dict) and 'order_post_result' in o:
                    runner = o
                    post = o.get('order_post_result') or {}
            if post and post.get('success') is True and str(post.get('status', '')).lower() == 'matched':
                token_id = str(runner.get('token_id') or token_id_pre)
                # Query ACTUAL on-chain balances rather than trusting post.takingAmount/makingAmount
                # (FAK partial fills make those fields report the REQUESTED amount, not the fill).
                # POLL for up to 10s: the order takes 1-2s to settle on-chain. We wait until the
                # balance INCREASES by more than dust (0.05 shares) over ctf_before_open, not just
                # until it's non-zero — pre-existing dust made the old check stop early.
                settle_deadline = time.time() + 10.0
                settle_started = time.time()
                ctf_after_open = get_ctf_balance(bal_client, token_id, errors=report.get('balance_errors'))
                ctf_after_open = ctf_after_open if ctf_after_open is not None else 0.0
                while ctf_after_open <= ctf_before_open + 0.05 and time.time() < settle_deadline:
                    time.sleep(0.5)
                    ctf_after_open = get_ctf_balance(bal_client, token_id, errors=report.get('balance_errors'))
                    ctf_after_open = ctf_after_open if ctf_after_open is not None else 0.0
                settle_wait_sec = round(time.time() - settle_started, 2)
                pusd_after_open = get_pusd_balance(bal_client, errors=report.get('balance_errors'))
                shares_req = float(post.get('takingAmount') or 0)
                cost_req = float(post.get('makingAmount') or 0)
                # No-fill handling: if the open reported "matched" but the actual
                # on-chain CTF balance is still ~0 (no shares landed), the order
                # didn't really fill. Don't fabricate an opened trade from SDK
                # amounts — that would log a fake ENTERED and later a bogus PnL.
                # Mark as no-fill and keep scanning for a real entry.
                if ctf_after_open <= 0.01:
                    report['attempts'].append({
                        'ts': ts_utc(),
                        'slug': slug,
                        'status': 'open_failed_or_no_fill',
                        'side': side,
                        'shares_requested': shares_req,
                        'cost_requested_usdc': cost_req,
                        'ctf_balance_after_open': ctf_after_open,
                        'settle_wait_sec': settle_wait_sec,
                        'seconds_left': sec_left,
                    })
                    report['last_open_try'] = out[-2000:]
                    time.sleep(args.poll_sec)
                    continue
                # If the balance increased but only to dust level (partial / pre-existing),
                # fall back to SDK amounts and flag it so the report is honest.
                balance_unconfirmed = (ctf_after_open <= ctf_before_open + 0.05)
                if balance_unconfirmed:
                    shares = shares_req
                    cost = cost_req
                else:
                    shares = ctf_after_open - ctf_before_open
                    if pusd_before_open is not None and pusd_after_open is not None:
                        cost = round(max(0.0, pusd_before_open - pusd_after_open), 6)
                    else:
                        cost = cost_req
                entry_price = float(runner.get('entry_price') or trigger_price)
                if shares > 0 and cost > 0:
                    entry_price = cost / shares
                opened = {
                    'opened_at': ts_utc(),
                    'market_slug': slug,
                    'market_end_iso': end_iso,
                    'side': side,
                    'token_id': token_id,
                    'entry_price': entry_price,
                    'shares': shares,
                    'shares_requested': shares_req,
                    'cost_usdc': cost,
                    'cost_requested_usdc': cost_req,
                    'ctf_balance_after_open': ctf_after_open,
                    'pusd_balance_after_open': pusd_after_open,
                    'settle_wait_sec': settle_wait_sec,
                    'balance_unconfirmed': balance_unconfirmed,
                    'open_order_id': post.get('orderID'),
                    'open_tx': (post.get('transactionsHashes') or [None])[0],
                }
                report['open_raw'] = out[-4000:]
                break
            else:
                report['last_open_try'] = out[-2000:]
        except Exception as e:
            report['attempts'].append({'ts': ts_utc(), 'status': 'error', 'error': str(e)})
        time.sleep(args.poll_sec)

    if not opened:
        report['finished_at'] = ts_utc()
        report['result'] = 'no_entry_timeout'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    report['opened'] = opened

    # monitor after open: stop-loss or time exit
    end_ts = None
    try:
        end_ts = dt.datetime.fromisoformat(opened['market_end_iso'].replace('Z', '+00:00')).timestamp()
    except Exception:
        end_ts = time.time() + 300

    sl_price = opened['entry_price'] * (1.0 - args.stop_loss_pct) if args.stop_loss_pct > 0 else None
    report['stop_loss_price'] = sl_price
    hold_to_settlement = (args.stop_loss_pct == 0)

    close_reason = None
    while True:
        now = time.time()
        # Hold-to-settlement mode: wait until the market FULLY ends, not 5s before.
        # Closing 5s before end fails for the losing side (no liquidity) and
        # leaves shares stranded — better to let the market resolve and auto-redeem.
        if hold_to_settlement:
            if now >= end_ts:
                close_reason = 'held_to_settlement'
                break
        else:
            if now >= (end_ts - args.exit_before_sec):
                close_reason = f'time_exit_{args.exit_before_sec}s_before_end'
                break

        # Stop-loss is disabled when stop_loss_pct == 0 (hold to settlement).
        # 5-min markets resolve themselves; a stop-loss just sells at the bid
        # on transient price swings, locking in the spread loss every trade.
        if sl_price is not None:
            side_px = get_side_price_from_slug(opened['market_slug'], opened['side'])
            report['last_side_price'] = side_px
            report['last_check_at'] = ts_utc()
            if side_px is not None and side_px <= sl_price:
                close_reason = f"stop_loss_{int(args.stop_loss_pct * 100)}pct"
                break
        time.sleep(args.poll_sec)

    close_debug: list[dict[str, Any]] = []
    close_obj: dict[str, Any] = {}
    out = ''
    fallback_used = None
    force_close_used = None
    client = auth_clob_client()

    # Hold-to-settlement path: skip the FAK close loop entirely.
    # Wait for the market to resolve and shares to auto-redeem (winning side
    # redeems at $1; losing side shares go to $0 / stay worthless). Then
    # compute PnL from the actual pUSD balance change. This avoids the FAK
    # close failing on the losing side (no liquidity 5s before end) and
    # recording pnl=$0 when the real loss is -$cost.
    if hold_to_settlement:
        # Polymarket's auto-redeem can take 90s+ after market resolution. The 90s
        # deadline was too short — SOL trade on 2026-06-20 redeemed at ~91s and the
        # bot gave up at 90s, logging a $8.13 loss when the position actually won
        # (+$4.64). 180s gives the redeem plenty of time to land; we break out as
        # soon as ctf_remaining hits 0, so this only delays logging when the
        # redeem genuinely takes longer than expected.
        redeem_deadline = time.time() + 180.0
        redeem_started = time.time()
        ctf_remaining = get_ctf_balance(client, opened['token_id'], errors=report.get('balance_errors'))
        ctf_remaining = ctf_remaining if ctf_remaining is not None else 0.0
        while ctf_remaining > 0.05 and time.time() < redeem_deadline:
            time.sleep(2.0)
            ctf_remaining = get_ctf_balance(client, opened['token_id'], errors=report.get('balance_errors'))
            ctf_remaining = ctf_remaining if ctf_remaining is not None else 0.0
        redeem_wait_sec = round(time.time() - redeem_started, 2)
        pusd_after_close = get_pusd_balance(client, errors=report.get('balance_errors'))
        shares_remaining = ctf_remaining
        # If CTF went to ~0 (< 0.05 shares = dust): won, shares redeemed at $1 each.
        # If CTF stayed > 0.05: lost, shares are worthless (no redemption).
        position_closed = ctf_remaining < 0.05
        actual_close_usdc = None
        if opened.get('pusd_balance_after_open') is not None and pusd_after_close is not None:
            actual_close_usdc = round(max(0.0, pusd_after_close - opened['pusd_balance_after_open']), 6)
        actual_close_shares = None
        if opened.get('ctf_balance_after_open') is not None and ctf_remaining is not None:
            actual_close_shares = round(max(0.0, opened['ctf_balance_after_open'] - ctf_remaining), 6)
        closed = {
            'close_reason': close_reason,
            'closed_at': ts_utc(),
            'close_success': position_closed,
            'close_status': 'redeemed' if position_closed else 'worthless',
            'close_order_id': None,
            'close_tx': None,
            'close_shares': actual_close_shares if actual_close_shares is not None else opened['shares'],
            'close_shares_requested': opened['shares'],
            'close_usdc': actual_close_usdc if actual_close_usdc is not None else 0.0,
            'close_usdc_requested': 0.0,
            'ctf_remaining': ctf_remaining,
            'pusd_balance_after_close': pusd_after_close,
            'position_closed_on_chain': position_closed,
            'close_settle_wait_sec': redeem_wait_sec,
            'close_skipped': '',
        }
        report['close_debug'] = [{'ts': ts_utc(), 'mode': 'hold_to_settlement', 'redeem_wait_sec': redeem_wait_sec, 'ctf_remaining': ctf_remaining}]
        report['closed'] = closed
        # PnL from actual pUSD delta: captures wins (+shares×$1 - cost) and losses (-cost).
        pnl = None
        if pusd_before_open is not None and pusd_after_close is not None:
            pnl = round(pusd_after_close - pusd_before_open, 6)
        elif actual_close_usdc is not None and opened['cost_usdc']:
            pnl = round(actual_close_usdc - opened['cost_usdc'], 6)
        report['realized_cashflow_pnl_usdc'] = pnl
        report['finished_at'] = ts_utc()
        report['result'] = 'done'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    for i in range(max(1, int(args.close_retry_max))):
        out, objs = run_close(
            args.repo,
            opened['market_slug'],
            opened['token_id'],
            opened['shares'],
            args.execute,
            close_order_type='FAK',
        )
        close_obj = objs[-1] if objs else {}
        post = close_obj.get('order_post_result') or {}
        status = str(post.get('status') or '').lower()
        skipped = str(close_obj.get('close_skipped') or '')
        close_debug.append({
            'ts': ts_utc(),
            'attempt': i + 1,
            'order_type': 'FAK',
            'status': status,
            'close_skipped': skipped,
        })
        if post.get('success') is True and status == 'matched':
            break
        # On-chain check: a "failed" FAK may have actually filled most of the position.
        if position_is_dust(client, opened['token_id'], errors=report.get('balance_errors')):
            close_debug[-1]['dust_close_detected'] = True
            break

        # common transient path right after open: token balance not yet visible
        if skipped == 'zero_effective_shares':
            time.sleep(float(args.close_retry_delay_sec))
            continue

        # fallback: if FAK has no instant match, try a GTC limit close near current side price
        txt = ((out or '') + '\n' + json.dumps(close_obj, ensure_ascii=False)).lower()
        if 'no orders found to match with fak order' in txt:
            px = get_side_price_from_slug(opened['market_slug'], opened['side'])
            if px is None:
                px = report.get('last_side_price')
            if px is None:
                px = opened['entry_price']
            bb = None
            try:
                bb = clob_best_bid(opened['token_id'])
            except Exception:
                bb = None
            limit_px = max(0.01, min(0.99, float((bb - 0.01) if bb is not None else px)))
            fallback_used = {'type': 'GTC_LIMIT', 'price': limit_px}
            out2, objs2 = run_close(
                args.repo,
                opened['market_slug'],
                opened['token_id'],
                opened['shares'],
                args.execute,
                close_order_type='GTC',
                close_limit_price=limit_px,
            )
            close_obj2 = objs2[-1] if objs2 else {}
            post2 = close_obj2.get('order_post_result') or {}
            status2 = str(post2.get('status') or '').lower()
            close_debug.append({
                'ts': ts_utc(),
                'attempt': i + 1,
                'order_type': 'GTC',
                'status': status2,
                'close_skipped': str(close_obj2.get('close_skipped') or ''),
                'limit_price': limit_px,
            })
            close_obj = close_obj2
            out = out2
            if post2.get('success') is True and status2 == 'matched':
                break
            if position_is_dust(client, opened['token_id'], errors=report.get('balance_errors')):
                close_debug[-1]['dust_close_detected'] = True
                break

            # If GTC is accepted but still live, force-close flow: poll status, cancel, repost aggressive.
            if post2.get('success') is True and status2 == 'live':
                oid2 = str(post2.get('orderID') or '')
                st_upd, ord_upd = poll_order_status(client, oid2, wait_sec=min(8.0, max(2.0, float(args.close_retry_delay_sec) * 2)), step_sec=1.0)
                close_debug.append({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'GTC_POLL',
                    'status': st_upd.lower() if st_upd else '',
                    'order_id': oid2,
                })
                if st_upd == 'MATCHED':
                    post2['status'] = 'matched'
                    close_obj['order_post_result'] = post2
                    break

                cancel_info = cancel_token_orders(client, opened['token_id'])
                bb2 = None
                try:
                    bb2 = clob_best_bid(opened['token_id'])
                except Exception:
                    bb2 = None
                force_px = max(0.01, min(0.99, float((bb2 - 0.02) if bb2 is not None else 0.01)))
                force_close_used = {
                    'type': 'FORCE_GTC_LIMIT',
                    'price': force_px,
                    'cancel_info': cancel_info,
                }
                out3, objs3 = run_close(
                    args.repo,
                    opened['market_slug'],
                    opened['token_id'],
                    opened['shares'],
                    args.execute,
                    close_order_type='GTC',
                    close_limit_price=force_px,
                )
                close_obj3 = objs3[-1] if objs3 else {}
                post3 = close_obj3.get('order_post_result') or {}
                status3 = str(post3.get('status') or '').lower()
                close_debug.append({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'FORCE_GTC',
                    'status': status3,
                    'close_skipped': str(close_obj3.get('close_skipped') or ''),
                    'limit_price': force_px,
                })
                close_obj = close_obj3
                out = out3
                if post3.get('success') is True and status3 == 'matched':
                    break
                if position_is_dust(client, opened['token_id'], errors=report.get('balance_errors')):
                    close_debug[-1]['dust_close_detected'] = True
                    break

        time.sleep(float(args.close_retry_delay_sec))

    post = close_obj.get('order_post_result') or {}
    post_status = str(post.get('status') or '').lower()
    close_usdc_req = float(post.get('takingAmount') or 0)
    close_shares_req = float(post.get('makingAmount') or 0)

    # Query ACTUAL on-chain state after the close loop, with a settle-wait.
    # The close order's on-chain effects (CTF burn + pUSD credit) take 1-2s to
    # settle. Querying immediately returns the PRE-close state, which makes
    # actual_close_usdc = 0 and position_closed = False even though the order
    # matched. Poll until the shares are burned (ctf drops below post-open
    # balance) AND pUSD increased (proceeds credited), up to 10s.
    ctf_after_open_ref = opened.get('ctf_balance_after_open') or 0.0
    pusd_after_open_ref = opened.get('pusd_balance_after_open')
    settle_deadline = time.time() + 10.0
    settle_started = time.time()
    ctf_remaining = get_ctf_balance(client, opened['token_id'], errors=report.get('balance_errors'))
    pusd_after_close = get_pusd_balance(client, errors=report.get('balance_errors'))
    ctf_remaining = ctf_remaining if ctf_remaining is not None else ctf_after_open_ref
    while time.time() < settle_deadline:
        shares_burned = ctf_remaining < max(0.01, ctf_after_open_ref - 0.05)
        proceeds_credited = (
            pusd_after_open_ref is None
            or pusd_after_close is None
            or pusd_after_close > pusd_after_open_ref + 0.01
        )
        if shares_burned and proceeds_credited:
            break
        time.sleep(0.5)
        ctf_remaining = get_ctf_balance(client, opened['token_id'], errors=report.get('balance_errors'))
        pusd_after_close = get_pusd_balance(client, errors=report.get('balance_errors'))
        ctf_remaining = ctf_remaining if ctf_remaining is not None else ctf_after_open_ref
    close_settle_wait_sec = round(time.time() - settle_started, 2)
    # Actual close proceeds = pUSD gained since the open.
    actual_close_usdc = None
    if opened.get('pusd_balance_after_open') is not None and pusd_after_close is not None:
        actual_close_usdc = round(max(0.0, pusd_after_close - opened['pusd_balance_after_open']), 6)
    # Actual shares closed = shares held after open minus shares remaining.
    actual_close_shares = None
    if opened.get('ctf_balance_after_open') is not None and ctf_remaining is not None:
        actual_close_shares = round(max(0.0, opened['ctf_balance_after_open'] - ctf_remaining), 6)

    position_closed = ctf_remaining is not None and ctf_remaining < 0.01

    closed = {
        'close_reason': close_reason,
        'closed_at': ts_utc(),
        'close_success': position_closed or bool(post.get('success') is True and post_status == 'matched'),
        'close_status': post.get('status'),
        'close_order_id': post.get('orderID'),
        'close_tx': (post.get('transactionsHashes') or [None])[0],
        'close_shares': actual_close_shares if actual_close_shares is not None else close_shares_req,
        'close_shares_requested': close_shares_req,
        'close_usdc': actual_close_usdc if actual_close_usdc is not None else close_usdc_req,
        'close_usdc_requested': close_usdc_req,
        'ctf_remaining': ctf_remaining,
        'pusd_balance_after_close': pusd_after_close,
        'position_closed_on_chain': position_closed,
        'close_settle_wait_sec': close_settle_wait_sec,
        'close_skipped': close_obj.get('close_skipped'),
    }
    report['close_debug'] = close_debug
    if fallback_used:
        report['close_fallback'] = fallback_used
    if force_close_used:
        report['close_force'] = force_close_used
    report['close_raw'] = out[-4000:]
    report['closed'] = closed

    # PnL: prefer actual on-chain pUSD delta (pusd_after_close - pusd_before_open)
    # which captures all fills, fees, and partial closes. Fall back to reported
    # close_usdc - cost only if on-chain balances are unavailable.
    pnl = None
    if position_closed and pusd_before_open is not None and pusd_after_close is not None:
        pnl = round(pusd_after_close - pusd_before_open, 6)
    elif closed['close_usdc'] and opened['cost_usdc']:
        pnl = round(closed['close_usdc'] - opened['cost_usdc'], 6)
    report['realized_cashflow_pnl_usdc'] = pnl
    report['finished_at'] = ts_utc()
    report['result'] = 'done'

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
