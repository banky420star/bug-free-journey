#!/usr/bin/env python3
"""BTC 5m Polymarket TUI — live wallet + market monitor.

Shows:
  WALLET      proxy address, signer, pUSD cash, open CTF position
  MARKET      current 5-min BTC up/down market slug, end time, seconds left
  PRICES      UP/DOWN best bid + ask from the CLOB V2 order book
  RUNNER      live trade process status (if running) + latest log path

Refreshes every N seconds. Keys: r=refresh now, q=quit.
"""
import argparse
import curses
import datetime as dt
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_SCRIPTS = SCRIPT_DIR / "skills" / "btc-5m-live" / "scripts"
REPO = SCRIPT_DIR / "pm-hl-conservative-plus-repo"
VENV_PY = REPO / ".venv" / "bin" / "python"


def _load_runner_module():
    """Import test_btc_5m_session_exit_sl.py as a module to reuse its helpers."""
    spec = importlib.util.spec_from_file_location(
        "btc5m_runner", str(SKILL_SCRIPTS / "test_btc_5m_session_exit_sl.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_env():
    env_file = os.environ.get("BTC5M_ENV_FILE") or str(REPO / ".env")
    if os.path.exists(env_file):
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)


def _short(addr: str, head: int = 6, tail: int = 4) -> str:
    if not addr:
        return "—"
    if len(addr) <= head + tail + 2:
        return addr
    return f"{addr[:head]}…{addr[-tail:]}"


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _fmt_shares(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "0"
    if v < 0.01:
        return f"{v:.6f}"
    return f"{v:.4f}"


def _fmt_countdown(sec_left: Optional[float]) -> str:
    if sec_left is None:
        return "—"
    if sec_left <= 0:
        return "ended"
    m = int(sec_left) // 60
    s = int(sec_left) % 60
    return f"{m}:{s:02d}"


def _runner_status() -> dict[str, Any]:
    """Check btc5m_ctl.pid + meta for a running live trade."""
    runtime = SCRIPT_DIR / "skills" / "btc-5m-live" / "runtime"
    pidfile = runtime / "btc5m.pid"
    metafile = runtime / "btc5m.meta.json"
    info: dict[str, Any] = {"running": False, "pid": None, "log": None, "profile": None}
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            if pid > 0 and _pid_alive(pid):
                info["running"] = True
                info["pid"] = pid
        except Exception:
            pass
    if metafile.exists():
        try:
            import json
            meta = json.loads(metafile.read_text())
            info["log"] = meta.get("log")
            info["profile"] = meta.get("profile")
            info["entry_timeout_min"] = meta.get("entryTimeoutMin")
        except Exception:
            pass
    return info


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _latest_attempt(log_path: Optional[str]) -> dict[str, Any]:
    """Parse the latest heartbeat / skip / entry from a live run log."""
    if not log_path or not os.path.exists(log_path):
        return {}
    try:
        import json
        import re
        txt = Path(log_path).read_text(errors="replace")
        m = re.search(r'\{\s*"started_at"', txt)
        if not m:
            return {}
        body = txt[m.start():]
        depth = 0
        end = 0
        for i, ch in enumerate(body):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        r = json.loads(body[:end])
        attempts = r.get("attempts") or []
        last = attempts[-1] if attempts else {}
        return {
            "opened": r.get("opened"),
            "last_status": last.get("status"),
            "last_slug": last.get("slug"),
            "last_seconds_left": last.get("seconds_left"),
            "last_clob_up_ask": last.get("clob_up_ask"),
            "last_clob_down_ask": last.get("clob_down_ask"),
            "result": r.get("result"),
            "attempts_count": len(attempts),
        }
    except Exception:
        return {}


def _log_tail(loop_lines: int = 15, trade_lines: int = 8) -> dict[str, Any]:
    """Tail the loop runner log + the latest trade log for the TUI LOG section."""
    result: dict[str, Any] = {"loop": [], "trade": [], "loop_path": None, "trade_path": None}
    runtime = SCRIPT_DIR / "skills" / "btc-5m-live" / "runtime"

    loop_log = runtime / "loop_runner.log"
    if loop_log.exists():
        result["loop_path"] = str(loop_log)
        try:
            lines = loop_log.read_text(errors="replace").splitlines()
            result["loop"] = lines[-loop_lines:]
        except Exception:
            pass

    # Latest trade log: follow the latest.log symlink, else newest btc5m_*.log
    trade_log = runtime / "latest.log"
    if not trade_log.exists():
        candidates = sorted(runtime.glob("btc5m_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            trade_log = candidates[0]
    if trade_log.exists():
        result["trade_path"] = str(trade_log)
        try:
            lines = trade_log.read_text(errors="replace").splitlines()
            result["trade"] = lines[-trade_lines:]
        except Exception:
            pass

    return result


def _seed_seen_trade_ts() -> Optional[str]:
    """Read the latest 'done' trade ts from loop_pnl.csv at TUI startup.

    Pre-seeds the popup seen-set so trades that already happened before the
    TUI launched don't trigger popups. Only trades that land AFTER startup popup.
    """
    csv_path = SCRIPT_DIR / "skills" / "btc-5m-live" / "runtime" / "loop_pnl.csv"
    if not csv_path.exists():
        return None
    try:
        import csv
        with open(csv_path, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("trade_result") == "done"]
        if not rows:
            return None
        return rows[-1].get("ts") or None
    except Exception:
        return None


def _recent_trades(n: int = 12) -> list[dict[str, Any]]:
    """Last N completed trades from loop_pnl.csv as clean dicts for the LOG section."""
    csv_path = SCRIPT_DIR / "skills" / "btc-5m-live" / "runtime" / "loop_pnl.csv"
    if not csv_path.exists():
        return []
    try:
        import csv
        rows = []
        with open(csv_path, newline="") as f:
            for r in csv.DictReader(f):
                rows.append(r)
        done = [r for r in rows if r.get("trade_result") == "done"]
        recent = done[-n:]
        out = []
        for r in recent:
            pnl = float(r.get("pnl") or 0)
            out.append({
                "ts": r.get("ts") or "",
                "asset": (r.get("asset") or "").upper(),
                "stake": float(r.get("stake") or 0),
                "result": r.get("trade_result") or "",
                "pnl": pnl,
                "side": (r.get("side") or "").upper(),
                "entry_price": float(r.get("entry_price") or 0),
            })
        return out
    except Exception:
        return []


def _loop_performance() -> dict[str, Any]:
    """Read runtime/loop_pnl.csv and compute cumulative performance."""
    csv_path = SCRIPT_DIR / "skills" / "btc-5m-live" / "runtime" / "loop_pnl.csv"
    if not csv_path.exists():
        return {"has_data": False}
    try:
        import csv
        rows = []
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        if not rows:
            return {"has_data": False}
        trades = [r for r in rows if r.get("trade_result") == "done"]
        wins = [r for r in trades if float(r.get("pnl") or 0) > 0]
        losses = [r for r in trades if float(r.get("pnl") or 0) < 0]
        flats = [r for r in trades if float(r.get("pnl") or 0) == 0]
        # Total PnL = balance delta (captures everything: trades, auto-redemptions,
        # killed trades — anything that moved the balance). The PnL-column sum
        # misses rows that were never logged as "done" trades.
        start_bal = float(rows[0].get("balance_before") or 0)
        last_bal = float(rows[-1].get("balance_after") or 0)
        total_pnl = last_bal - start_bal
        last_pnl = float(trades[-1].get("pnl") or 0) if trades else None
        last_side = None
        if trades:
            last_side = trades[-1].get("side")
        return {
            "has_data": True,
            "total_trades": len(trades),
            "total_iterations": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "flats": len(flats),
            "total_pnl": total_pnl,
            "start_balance": start_bal,
            "last_balance": last_bal,
            "last_pnl": last_pnl,
            "win_rate": (len(wins) / len(trades) * 100) if trades else 0,
            "avg_pnl": (total_pnl / len(trades)) if trades else 0,
            "best_pnl": max((float(r.get("pnl") or 0) for r in trades), default=0),
            "worst_pnl": min((float(r.get("pnl") or 0) for r in trades), default=0),
        }
    except Exception:
        return {"has_data": False}


def _per_asset_performance() -> dict[str, dict[str, Any]]:
    """Per-asset PnL breakdown from loop_pnl.csv. Returns {asset: {trades, wins, losses, pnl, last_pnl, streak, boost}}."""
    csv_path = SCRIPT_DIR / "skills" / "btc-5m-live" / "runtime" / "loop_pnl.csv"
    out: dict[str, dict[str, Any]] = {
        a: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "last_pnl": None, "streak": 0, "boost": 0.0}
        for a in ASSETS
    }
    if not csv_path.exists():
        return out
    try:
        import csv
        rows_by_asset: dict[str, list] = {a: [] for a in ASSETS}
        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("trade_result") != "done":
                    continue
                asset = (row.get("asset") or "").lower()
                if asset not in rows_by_asset:
                    continue
                rows_by_asset[asset].append(row)
        for asset, rows in rows_by_asset.items():
            for r in rows:
                pnl = float(r.get("pnl") or 0)
                out[asset]["trades"] += 1
                out[asset]["pnl"] += pnl
                if pnl > 0:
                    out[asset]["wins"] += 1
                elif pnl < 0:
                    out[asset]["losses"] += 1
                out[asset]["last_pnl"] = pnl
            # Compute current win streak (consecutive winning trades from the end)
            streak = 0
            for r in reversed(rows):
                if float(r.get("pnl") or 0) > 0:
                    streak += 1
                else:
                    break
            out[asset]["streak"] = streak
            # Boost = (streak // 3) * avg_win (matches run_to_target.sh logic)
            if streak >= 3:
                streak_wins = [r for r in rows[-streak:] if float(r.get("pnl") or 0) > 0]
                avg_win = sum(float(r["pnl"]) for r in streak_wins) / len(streak_wins)
                out[asset]["boost"] = (streak // 3) * avg_win
    except Exception:
        pass
    return out


ASSETS = ["btc", "eth", "sol", "xrp", "doge"]


def _fetch_asset_prices(runner_module, asset: str) -> dict[str, Any]:
    """Fetch current 5m market + CLOB bid/ask for one asset."""
    info: dict[str, Any] = {
        "asset": asset, "slug": None, "end_iso": None, "sec_left": None,
        "up_bid": None, "up_ask": None, "down_bid": None, "down_ask": None,
        "up_token": None, "down_token": None,
        "in_band": None,  # "UP" / "DOWN" / None
    }
    try:
        mkt = runner_module.resolve_active_current_5m_market(asset)
    except Exception:
        mkt = None
    if not mkt:
        return info
    try:
        g_up, g_dn, up_t, dn_t, slug, end_iso = runner_module.market_side_prices(mkt)
        info["slug"] = slug
        info["end_iso"] = end_iso
        info["up_token"] = up_t
        info["down_token"] = dn_t
        if end_iso:
            try:
                end_ts = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp()
                info["sec_left"] = max(0.0, end_ts - time.time())
            except Exception:
                pass
        import requests
        for label, tok in [("UP", up_t), ("DOWN", dn_t)]:
            if not tok:
                continue
            try:
                book = requests.get(f"https://clob.polymarket.com/book?token_id={tok}", timeout=4).json()
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                bid = float(bids[0]["price"]) if bids else None
                ask = float(asks[0]["price"]) if asks else None
                if label == "UP":
                    info["up_bid"], info["up_ask"] = bid, ask
                else:
                    info["down_bid"], info["down_ask"] = bid, ask
            except Exception:
                pass
        # Check which side is in the entry band (0.62-0.75)
        ua, da = info["up_ask"], info["down_ask"]
        if ua is not None and 0.62 <= ua <= 0.75:
            info["in_band"] = "UP"
        elif da is not None and 0.62 <= da <= 0.75:
            info["in_band"] = "DOWN"
    except Exception:
        pass
    return info


def gather_state(runner_module) -> dict[str, Any]:
    """Collect all data for one TUI frame."""
    from concurrent.futures import ThreadPoolExecutor

    client = runner_module.auth_clob_client()

    # Fetch pUSD + all 5 assets' markets/prices in parallel (cuts ~40s serial → ~8s)
    with ThreadPoolExecutor(max_workers=6) as pool:
        pusd_fut = pool.submit(runner_module.get_pusd_balance, client)
        asset_futs = {a: pool.submit(_fetch_asset_prices, runner_module, a) for a in ASSETS}
        pusd = pusd_fut.result()
        assets_info = [asset_futs[a].result() for a in ASSETS]

    # Use BTC as the "primary" market for backwards compat
    btc_info = assets_info[0] if assets_info else {}
    slug = btc_info.get("slug")
    end_iso = btc_info.get("end_iso")
    sec_left = btc_info.get("sec_left")
    up_bid = btc_info.get("up_bid")
    up_ask = btc_info.get("up_ask")
    down_bid = btc_info.get("down_bid")
    down_ask = btc_info.get("down_ask")

    # Open position: reuse tokens already fetched in assets_info (no re-resolve)
    position_token = None
    position_shares = None
    position_side = None
    position_asset = None
    position_sec_left = None
    for ai in assets_info:
        up_t = ai.get("up_token")
        dn_t = ai.get("down_token")
        for label, tok in [("UP", up_t), ("DOWN", dn_t)]:
            if not tok:
                continue
            try:
                bal = runner_module.get_ctf_balance(client, tok)
            except Exception:
                bal = None
            if bal and bal > 0.001:
                position_token, position_shares, position_side = tok, bal, label
                position_asset = ai["asset"]
                position_sec_left = ai.get("sec_left")
                break
        if position_shares:
            break

    runner = _runner_status()
    runner["attempt"] = _latest_attempt(runner.get("log"))

    performance = _loop_performance()
    per_asset_perf = _per_asset_performance()
    log_tail = _log_tail()
    recent_trades = _recent_trades(12)

    return {
        "proxy": os.getenv("PM_FUNDER") or os.getenv("PM_ADDRESS") or "",
        "signer_sig": os.getenv("PM_SIGNATURE_TYPE", "1"),
        "pusd": pusd,
        "slug": slug,
        "end_iso": end_iso,
        "sec_left": sec_left,
        "up_bid": up_bid, "up_ask": up_ask,
        "down_bid": down_bid, "down_ask": down_ask,
        "position_side": position_side,
        "position_shares": position_shares,
        "position_asset": position_asset,
        "position_sec_left": position_sec_left,
        "assets_info": assets_info,
        "runner": runner,
        "performance": performance,
        "per_asset_perf": per_asset_perf,
        "log_tail": log_tail,
        "recent_trades": recent_trades,
        "fetched_at": dt.datetime.now(dt.timezone.utc),
    }


def render(stdscr, state: dict[str, Any], error: Optional[str], popups: Optional[list] = None) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    col_w = w // 2
    right_x = col_w

    # Two independent column cursors
    cols = {"left": 0, "right": 0}

    def line(col: str, text: str = "", attr=curses.A_NORMAL, wrap: bool = True) -> None:
        x = 0 if col == "left" else right_x
        cw = col_w if col == "left" else (w - right_x)
        y = cols[col]
        if y >= h - 1:
            return
        if wrap and len(text) > cw - 1:
            text = text[: cw - 1]
        try:
            stdscr.addnstr(y, x, text, cw - 1, attr)
        except curses.error:
            pass
        cols[col] = y + 1

    def line_segs(col: str, segs: list[tuple[str, int]]) -> None:
        """Write a line made of (text, attr) segments. Each segment keeps its own
        color — used so we don't color the whole row when only one tag should pop."""
        x0 = 0 if col == "left" else right_x
        cw = col_w if col == "left" else (w - right_x)
        y = cols[col]
        if y >= h - 1:
            return
        cx = x0
        for text, attr in segs:
            if cx - x0 >= cw - 1:
                break
            room = cw - 1 - (cx - x0)
            if len(text) > room:
                text = text[:room]
            try:
                stdscr.addstr(y, cx, text, attr)
            except curses.error:
                pass
            cx += len(text)
        cols[col] = y + 1

    # Header (spans full width)
    try:
        stdscr.addnstr(0, 0, "  BTC 5M LIVE  —  Polymarket Trading TUI", w - 1, curses.A_BOLD | curses.A_REVERSE)
        stdscr.addnstr(1, 0, f"  {state['fetched_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}    run: ./run_to_target.sh --max-trades 500", w - 1)
    except curses.error:
        pass
    cols["left"] = 2
    cols["right"] = 2

    # Vertical separator
    try:
        vline = curses.ACS_VLINE
    except AttributeError:
        vline = ord('|')
    for yy in range(2, h - 1):
        try:
            stdscr.addch(yy, right_x - 1, vline, curses.color_pair(3) | curses.A_DIM)
        except (curses.error, AttributeError):
            pass

    if error:
        line("left", f"  ERROR: {error}", curses.color_pair(2) | curses.A_BOLD)
        line("left", "")
        return

    # === LEFT COLUMN: WALLET, PER-ASSET SECTIONS, TOTAL ===

    # WALLET (compact)
    line("left", "  WALLET", curses.A_BOLD | curses.color_pair(3))
    line("left", f"    Proxy:    {_short(state['proxy'])}")
    line("left", f"    pUSD:     {_fmt_usd(state['pusd'])}")
    pos = state["position_shares"]
    if pos and pos > 0.001:
        line("left", f"    Position: {_fmt_shares(pos)} sh {state['position_side']}",
             curses.color_pair(1) | curses.A_BOLD)
    elif pos is not None and pos > 0:
        line("left", f"    Position: {_fmt_shares(pos)} sh {state['position_side']} (dust)",
             curses.color_pair(4))
    else:
        line("left", "    Position: none")
    line("left", "")

    # PER-ASSET SECTIONS — each asset shows market countdown, boost, in-trade status, PnL
    per_asset = state.get("per_asset_perf") or {}
    pos_asset = state.get("position_asset")
    pos_side = state.get("position_side")
    pos_shares = state.get("position_shares")
    pos_sec_left = state.get("position_sec_left")
    line("left", "  PER-ASSET  (market / boost / in-trade / PnL)", curses.A_BOLD | curses.color_pair(3))
    for ai in state.get("assets_info") or []:
        asset_upper = ai["asset"].upper()
        asset_lc = ai["asset"]
        sl = ai.get("sec_left")
        cd = _fmt_countdown(sl) if sl is not None else "—"
        ap = per_asset.get(asset_lc, {})
        boost = ap.get("boost", 0.0)
        streak = ap.get("streak", 0)
        trades = ap.get("trades", 0)
        wins = ap.get("wins", 0)
        losses = ap.get("losses", 0)
        pnl = ap.get("pnl", 0.0)
        last_pnl = ap.get("last_pnl")

        # Line 1: asset + market countdown + streak/boost
        # Keep the base line dim; only color the boost value green when boost > 0.
        base_attr = curses.A_DIM
        boost_str = f"boost ${boost:.2f}"
        boost_attr = curses.color_pair(1) | curses.A_BOLD if boost > 0 else curses.A_DIM
        line_segs("left", [
            (f"    {asset_upper:<5} mkt {cd:>4}  streak {streak}  ", base_attr),
            (boost_str, boost_attr),
        ])

        # Line 2: in-trade status (if this asset has the open position)
        if pos_asset == asset_lc and pos_shares and pos_shares > 0.001:
            settle_cd = _fmt_countdown(pos_sec_left)
            trade_attr = curses.color_pair(1) | curses.A_BOLD if pos_side == "UP" else curses.color_pair(2) | curses.A_BOLD
            line("left", f"          IN TRADE: {pos_side} {_fmt_shares(pos_shares)}sh  settles in {settle_cd}", trade_attr)
        elif pos_asset == asset_lc and pos_shares and pos_shares > 0:
            line("left", f"          dust {_fmt_shares(pos_shares)}sh {pos_side}", curses.color_pair(4))
        else:
            band = ai.get("in_band")
            if band:
                # Only color the UP/DOWN tag yellow — rest of the line stays dim.
                tag_attr = curses.color_pair(4) | curses.A_BOLD
                line_segs("left", [
                    (f"          ", curses.A_DIM),
                    (band, tag_attr),
                    (" in entry band (0.62-0.75)", curses.A_DIM),
                ])
            else:
                line("left", f"          (no position)", curses.A_DIM)

        # Line 3: PnL summary — only color the PnL number, rest stays dim
        if pnl > 0:
            pnl_str = f"+${pnl:.2f}"
            pnl_attr = curses.color_pair(1) | curses.A_BOLD
        elif pnl < 0:
            pnl_str = f"-${abs(pnl):.2f}"
            pnl_attr = curses.color_pair(2) | curses.A_BOLD
        else:
            pnl_str = "$0.00"
            pnl_attr = curses.color_pair(4)  # flat = yellow, dim-ish
        last_str = ""
        if last_pnl is not None:
            if last_pnl > 0:
                last_str = f"  last +${last_pnl:.2f}"
            elif last_pnl < 0:
                last_str = f"  last -${abs(last_pnl):.2f}"
            else:
                last_str = "  last $0.00"
        line_segs("left", [
            ("          PnL ", curses.A_DIM),
            (pnl_str, pnl_attr),
            (f"  {wins}W/{losses}L{last_str}", curses.A_DIM),
        ])
    line("left", "")

    # TOTAL (aggregate performance)
    perf = state.get("performance") or {}
    line("left", "  TOTAL", curses.A_BOLD | curses.color_pair(3))
    if not perf.get("has_data"):
        line("left", "    no trades logged yet")
    else:
        total_pnl = perf["total_pnl"]
        pnl_attr = curses.color_pair(1) | curses.A_BOLD if total_pnl >= 0 else curses.color_pair(2) | curses.A_BOLD
        wins = perf["wins"]
        losses = perf["losses"]
        wr_attr = curses.color_pair(1) if wins >= losses else curses.color_pair(2)
        line("left", f"    Trades:   {perf['total_trades']} done ({perf['total_iterations']} iter)")
        wr = perf["win_rate"]
        line("left", f"    W/L/F:    {wins}W/{losses}L/{perf['flats']}F  (wr {wr:.0f}%)", wr_attr)
        line("left", f"    Total PnL:{_fmt_usd(total_pnl)}  avg {_fmt_usd(perf['avg_pnl'])}", pnl_attr)
        line("left", f"    Best:     +{_fmt_usd(perf['best_pnl'])[1:]}", curses.color_pair(1))
        line("left", f"    Worst:    {_fmt_usd(perf['worst_pnl'])}", curses.color_pair(2))
        line("left", f"    Balance:  {_fmt_usd(perf['start_balance'])} → {_fmt_usd(perf['last_balance'])}")
    line("left", "")

    # === RIGHT COLUMN: RUNNER, LOG ===

    # RUNNER
    line("right", "  RUNNER", curses.A_BOLD | curses.color_pair(3))
    r = state["runner"]
    if r["running"]:
        line("right", f"    Status: RUNNING pid={r['pid']}", curses.color_pair(1) | curses.A_BOLD)
        line("right", f"    Profile: {r.get('profile') or '—'}")
        if r.get("log"):
            line("right", f"    Log: {Path(r['log']).name}")
        att = r.get("attempt") or {}
        if att:
            if att.get("opened"):
                o = att["opened"]
                line("right", f"    Open: {o.get('side')} {_fmt_shares(o.get('shares'))}sh @ {o.get('entry_price')}")
            last = att.get("last_status")
            if last:
                line("right", f"    Tick: {last} ({_fmt_countdown(att.get('last_seconds_left'))} left)")
            if att.get("result"):
                line("right", f"    Result: {att['result']}", curses.A_BOLD)
    else:
        line("right", "    Status: stopped", curses.color_pair(4))
        line("right", "    (no active runner — start with ./run_to_target.sh)", curses.A_DIM)
    line("right", "")

    # LOG (live tail) — clean: time, asset, side, stake, entry, result
    line("right", "  LOG (recent trades)", curses.A_BOLD | curses.color_pair(3))
    line("right", f"    {'TIME':<9}{'ASSET':<5}{'SIDE':<6}{'STAKE':>6}{'ENTRY':>6}  {'RESULT':>9}", curses.A_DIM)
    recent = state.get("recent_trades") or []
    if recent:
        for t in recent:
            ts = t["ts"][11:19] if len(t["ts"]) >= 19 else t["ts"]  # HH:MM:SS
            asset = t["asset"]
            side = t["side"] or "—"
            stake = t["stake"]
            entry = t["entry_price"]
            pnl = t["pnl"]
            entry_str = f"{entry:.2f}" if entry > 0 else "—"
            if abs(pnl) < 0.01:
                res_str = "$0.00"
                attr = curses.color_pair(4)
            elif pnl > 0:
                res_str = f"+${pnl:.2f}"
                attr = curses.color_pair(1) | curses.A_BOLD
            else:
                res_str = f"-${abs(pnl):.2f}"
                attr = curses.color_pair(2) | curses.A_BOLD
            line("right", f"    {ts:<9}{asset:<5}{side:<6}{_fmt_usd(stake):>6}{entry_str:>6}  {res_str:>9}", attr)
    else:
        line("right", "    (no trades yet)")

    # Popups (bottom-right, above the footer) — figlet banner + trade info, filled bg
    if popups:
        # Figlet-style "big" font banners (6 rows tall, ~30 chars wide)
        BANNER_WON = [
            "██╗    ██╗ ██████╗ ███╗   ██╗",
            "██║    ██║██╔═══██╗████╗  ██║",
            "██║ █╗ ██║██║   ██║██╔██╗ ██║",
            "██║███╗██║██║   ██║██║╚██╗██║",
            "╚███╔███╔╝╚██████╔╝██║ ╚████║",
            " ╚══╝╚══╝  ╚═════╝ ╚═╝  ╚═══╝",
        ]
        BANNER_LOST = [
            "██╗      ██████╗ ███████╗████████╗",
            "██║     ██╔═══██╗██╔════╝╚══██╔══╝",
            "██║     ██║   ██║███████╗   ██║   ",
            "██║     ██║   ██║╚════██║   ██║   ",
            "███████╗╚██████╔╝███████║   ██║   ",
            "╚══════╝ ╚═════╝ ╚══════╝   ╚═╝   ",
        ]
        BANNER_FLAT = [
            "██████╗ ███████╗████████╗",
            "██╔══██╗██╔════╝╚══██╔══╝",
            "██████╔╝█████╗     ██║      ",
            "██╔═══╝ ██╔══╝     ██║      ",
            "██║     ███████╗   ██║      ",
            "╚═╝     ╚══════╝   ╚═╝      ",
        ]

        FULL_W = 40           # full width (inside the borders)
        FULL_H = 11            # 2 borders + 6 banner + 1 blank + 1 trade-info + 1 footer-line
        SLIDE_MS = 250         # slide-in duration
        now = time.time()
        n = len(popups)
        # Stack upward from just above the footer; newest at the bottom
        start_y = h - 2 - (n * FULL_H)
        for i, p in enumerate(popups):
            y = start_y + i * FULL_H
            if y < 2 or y + FULL_H - 1 >= h - 1:
                continue
            # Slide-in from right edge: shift x from (w - 2) → (w - FULL_W - 2) over SLIDE_MS
            age_ms = (now - p["born_at"]) * 1000.0
            if age_ms < SLIDE_MS:
                t = age_ms / SLIDE_MS
                t = 1 - (1 - t) ** 3   # ease-out
                x = int((w - 2) - t * FULL_W)
            else:
                x = w - FULL_W - 2
            cur_w = FULL_W
            if x < right_x + 1:
                x = right_x + 1
                cur_w = w - x - 2

            # Pick color pair (filled background)
            pnl = p["pnl"]
            if abs(pnl) < 0.01:
                bg_pair = curses.color_pair(7)    # yellow bg, black text
                banner = BANNER_FLAT
                pnl_str = "$0.00"
            elif pnl > 0:
                bg_pair = curses.color_pair(5)    # green bg, white text
                banner = BANNER_WON
                pnl_str = f"+${pnl:.2f}"
            else:
                bg_pair = curses.color_pair(6)    # red bg, white text
                banner = BANNER_LOST
                pnl_str = f"-${abs(pnl):.2f}"

            side_str = p["side"] or ""
            inner_w = cur_w - 2  # subtract borders

            def pad(s: str, total: int = inner_w) -> str:
                s = s[:total]
                if len(s) < total:
                    half = (total - len(s)) // 2
                    return " " * half + s + " " * (total - len(s) - half)
                return s

            # Banner rows (centered within inner_w)
            banner_rows = [pad(r) for r in banner]
            # Trade info line
            trade_info = pad(f"{p['asset']} {side_str}   RESULT: {pnl_str}")
            # Returning-to-dashboard line
            returning = pad("returning to dashboard...")

            content_rows = banner_rows + [pad("")] + [trade_info] + [returning]  # 9 rows

            # Top border
            try:
                stdscr.addstr(y, x, "┌" + "─" * inner_w + "┐", bg_pair | curses.A_BOLD)
            except curses.error:
                pass
            # Content rows (all with filled colored background)
            for ry, content in enumerate(content_rows, start=1):
                try:
                    stdscr.addstr(y + ry, x, "│" + content + "│", bg_pair | curses.A_BOLD)
                except curses.error:
                    pass
            # Bottom border
            try:
                stdscr.addstr(y + 10, x, "└" + "─" * inner_w + "┘", bg_pair | curses.A_BOLD)
            except curses.error:
                pass

    # Footer (spans full width, bottom row)
    try:
        stdscr.addnstr(h - 1, 0, "  [r] refresh now   [q] quit", w - 1, curses.A_DIM)
    except curses.error:
        pass


def main_loop(stdscr, runner_module, interval: float) -> None:
    curses.curs_set(0)
    # Render at ~15fps so popups can animate; state refresh stays on `interval`.
    RENDER_INTERVAL_MS = 80
    stdscr.timeout(RENDER_INTERVAL_MS)
    try:
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)    # good / signal (fg only)
        curses.init_pair(2, curses.COLOR_RED, -1)      # error (fg only)
        curses.init_pair(3, curses.COLOR_CYAN, -1)     # section header (fg only)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)   # warning / dust (fg only)
        # Filled-background pairs for popups (white text on colored bg)
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_GREEN)   # win
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_RED)     # loss
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_YELLOW) # flat
    except Exception:
        pass

    state: dict[str, Any] = {}
    error: Optional[str] = None
    last_fetch = 0.0
    # Popup notification state: track the latest trade ts we've already shown,
    # and a list of active popups (each expires after POPUP_TTL seconds).
    POPUP_TTL = 6.0
    # Pre-seed seen-trades from the CSV at startup so old trades don't trigger
    # popups. Only trades that land AFTER the TUI starts will popup.
    last_seen_trade_ts: Optional[str] = _seed_seen_trade_ts()
    popups: list[dict[str, Any]] = []

    while True:
        now = time.time()
        if not state or now - last_fetch >= interval:
            try:
                state = gather_state(runner_module)
                error = None
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            last_fetch = now

            # Check for newly-completed trades and queue popups
            recent = state.get("recent_trades") or []
            if recent:
                latest_ts = recent[-1]["ts"]
                if last_seen_trade_ts is None:
                    # CSV was empty at startup — first trade seen is new, popup it
                    for t in reversed(recent):
                        popups.append({
                            "asset": t["asset"],
                            "side": t.get("side") or "",
                            "pnl": t["pnl"],
                            "born_at": now,
                            "expires_at": now + POPUP_TTL,
                        })
                    last_seen_trade_ts = latest_ts
                elif latest_ts != last_seen_trade_ts:
                    # Find trades newer than last_seen and queue popups for each
                    for t in reversed(recent):
                        if t["ts"] == last_seen_trade_ts:
                            break
                        popups.append({
                            "asset": t["asset"],
                            "side": t.get("side") or "",
                            "pnl": t["pnl"],
                            "born_at": now,
                            "expires_at": now + POPUP_TTL,
                        })
                    last_seen_trade_ts = latest_ts

        # Drop expired popups
        popups = [p for p in popups if now < p["expires_at"]]

        render(stdscr, state, error, popups)

        ch = stdscr.getch()
        if ch == ord("q") or ch == ord("Q"):
            break
        if ch == ord("r") or ch == ord("R"):
            last_fetch = 0.0  # force refresh


def main() -> int:
    ap = argparse.ArgumentParser(description="BTC 5m Polymarket TUI")
    ap.add_argument("--interval", type=float, default=10.0, help="refresh interval sec")
    args = ap.parse_args()

    if not VENV_PY.exists():
        print(f"venv not found: {VENV_PY}", file=sys.stderr)
        return 1

    _load_env()
    try:
        runner_module = _load_runner_module()
    except Exception as e:
        print(f"failed to load runner module: {e}", file=sys.stderr)
        return 1

    try:
        curses.wrapper(lambda stdscr: main_loop(stdscr, runner_module, args.interval))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())