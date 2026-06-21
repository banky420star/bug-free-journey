# Workspace setup — 5min BTC Polymarket skill

## Layout
```
/Volumes/AI_DRIVE/polly/                          # workspace root
├── SETUP.md                                       # this file
├── run_live.sh                                    # one-shot live pipeline wrapper (stops + starts)
├── watch_live.sh                                  # colorized tail + raw log follow
├── skills/btc-5m-live/                           # the skill (cloned from github)
│   ├── scripts/  config/  examples/  runtime/
│   ├── SKILL.md  CONTOUR.md  README.md
│   └── scripts/tail_trades.py                     # grey/green/red trade tail
└── pm-hl-conservative-plus-repo/                 # trading engine workspace
    ├── .venv/                                    # py-clob-client-v2, requests, dotenv, pyyaml
    ├── .env                                      # credentials (gitignored, filled)
    └── src/live/pm_live_trade_runner.py           # real CLOB V2 trade runner (built here)
```

## Status — verified working (CLOB V2 / pUSD)
- venv imports `py_clob_client_v2`, `requests`, `yaml`, `dotenv` ✅
- skill runner resolves the live BTC 5m market from Polymarket Gamma API ✅
- CLOB L2 auth: derived API creds bound to signer `0x8da37c6f…` + funder `0x2302…` + sig=1 ✅
  (`get_orders` returns `[]` — auth valid)
- trade runner loads `.env`, fetches V2 order book, computes size, emits correct JSON shape ✅
- end-to-end dry-run through `test_btc_5m_session_exit_sl.py` ✅
- **live order submit + cancel verified on V2 with sig=1 (POLY_PROXY)** ✅
- **maker-amount precision fix** (V2 caps maker ≤2 decimals, taker ≤4) ✅
- **first real $5 live fire attempted** (2026-06-20): runner entered the polling loop,
  saw qualifying signals, posted orders. Order rejected for maker precision → fixed.
  Re-launched with the fix in place.

> **V2 cutover (Apr 2026):** Polymarket deprecated CLOB V1. `py-clob-client==0.34.6`
> permanently returns `invalid order version, please use the latest clob-client`.
> This workspace uses `py-clob-client-v2==1.0.1`. Collateral is pUSD
> (`0xC011a7E1…342E82DFB`), not USDC.e — V1 queries showed $0 balance because they
> read the wrong token.

## Credentials in `.env`
- `PM_PRIVATE_KEY` = EVM key for signer `0x8da37c6f…`
- `PM_FUNDER`      = `0x2302703692dFb6d4F7C02Cc32A36A6e75cAdA4D2` (Polymarket proxy/Safe)
- `PM_SIGNATURE_TYPE` = **1** (V2: POLY_PROXY — works for this proxy.
  `2`=POLY_GNOSIS_SAFE fails with "invalid POLY_GNOSIS_SAFE signature"; `0`=EOA; `3`=POLY_1271)
- `PM_API_KEY` / `PM_API_SECRET` / `PM_API_PASSPHRASE` = blank → auto-derived via
  `ClobClient.create_or_derive_api_key()` (deterministic from signer key). Web-UI keys
  pasted earlier were bound to the Magic Link signer and returned 401; derived ones work.

## ⚠️ SECURITY — read before any real trade
The private key `0x4b85…` and all API keys were pasted in chat during setup and are
**compromised**. Before funding or running `--execute`:
1. Generate a fresh EVM wallet (new private key you never share).
2. Connect it to Polymarket to create a new proxy wallet, OR rotate the signer on the
   existing proxy.
3. Re-derive L2 creds from the new signer: `.venv/bin/python -c "from py_clob_client_v2 import ClobClient; c=ClobClient(host='https://clob.polymarket.com', chain_id=137, key='NEW_KEY', signature_type=1, funder='NEW_FUNDER'); print(c.create_or_derive_api_key())"`
4. Overwrite `pm-hl-conservative-plus-repo/.env` with the new `PM_PRIVATE_KEY`,
   `PM_FUNDER`, and derived `PM_API_*` values.
5. Revoke every API key shown in the Polymarket UI that was pasted in chat.

## Funding
The funder `0x2302…` must hold **pUSD** (Polymarket wrapped USDC,
`0xC011a7E1…342E82DFB`) on Polygon and have CLOB/CTF approvals. Polymarket sets
approvals automatically when you deposit via the Polymarket UI. If the proxy has no
pUSD, the first `--execute` order will fail at the exchange contract.

## Min order size & maker-amount precision
V2 order books report `min_order_size` (currently **5 shares** for BTC 5m markets).
V2 also enforces **maker amount ≤ 2 decimals** and **taker amount ≤ 4 decimals**:

- maker = `size × price` (the pUSD you spend on a BUY)
- taker = `size` (the shares you receive)

For tick = `0.01` (price has 2 decimals), `size` must be an **integer** for `size × price`
to land on 2 decimals. The runner floors `size` accordingly via
`round_size_for_maker()` in `pm_live_trade_runner.py`. Examples with `stake_usd=5`:

| price | raw size | floored size | cost (maker) |
|------:|---------:|-------------:|-------------:|
| 0.99  | 5.05     | 5            | $4.95        |
| 0.70  | 7.14     | 7            | $4.90        |
| 0.50  | 10.00    | 10           | $5.00        |
| 0.95  | 5.26     | 5            | $4.75        |

Cost comes in slightly under budget — saves a few cents per trade. If the floored size
somehow still produces a non-2-decimal maker (shouldn't happen for tick ≥ 0.01), the
runner emits `status="skip_maker_precision"` and posts nothing.

The runner emits `status="skip_size_below_min"` if the budget is too small for the
current price even after flooring; bump `stake_usd` to fix.

## Run

**Recommended — one-shot live pipeline** (stops any existing run, starts a fresh live trade, logs to `runtime/btc5m_<profile>_<ts>.log` so the colorized tail picks it up):
```
./run_live.sh                                   # conservative, 5-min entry window, $5 stake
./run_live.sh --threshold 0.62                  # less picky threshold
./run_live.sh --entry-timeout-min 30            # watch ~6 slots instead of 1
./run_live.sh --profile aggressive --stake-usd 10
```
⚠️ `run_live.sh` ALWAYS passes `--execute` (REAL pUSD orders).

**Watch live trades** (grey ENTERED / green WON / red LOST):
```
./watch_live.sh --follow                        # colorized tail + tail -f latest log
pm-hl-conservative-plus-repo/.venv/bin/python skills/btc-5m-live/scripts/tail_trades.py --follow
```

Dry-run (no orders, safe):
```
pm-hl-conservative-plus-repo/.venv/bin/python \
  skills/btc-5m-live/scripts/test_btc_5m_session_exit_sl.py --profile conservative
```

Live (REAL orders — only after rotating creds + funding):
```
pm-hl-conservative-plus-repo/.venv/bin/python \
  skills/btc-5m-live/scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Control script — WARNING: `btc5m_ctl.sh start` ALWAYS passes `--execute` (real orders):
```
skills/btc-5m-live/scripts/btc5m_ctl.sh start --profile conservative
skills/btc-5m-live/scripts/btc5m_ctl.sh status
skills/btc-5m-live/scripts/btc5m_ctl.sh report --limit 20
skills/btc-5m-live/scripts/btc5m_ctl.sh stop
```

## Entry timeout
Default `entry_timeout_min=5` (one BTC 5-min slot). The runner polls the current 5m
market each tick; skips if `<60s` remain (`min_entry_seconds_left`); skips if neither
side's best ask ≥ threshold. If no qualifying signal appears within 5 min, exits with
`result="no_entry_timeout"` and posts no order. Override with `--entry-timeout-min N`
to watch multiple slots.

## Trade runner contract (for `pm_live_trade_runner.py`)
OPEN: `--market-slug S --force-side UP|DOWN --start-equity N --risk-frac F --max-notional-usd U [--execute]`
  → BUY at best ask, capped by `--max-notional-usd`. Emits `{"order_post_result": …, "token_id", "entry_price", "side", …}`.
CLOSE: `--market-slug S --close-token-id T --close-shares N [--close-limit-price P] [--execute]`
  → SELL at best bid (or limit). Emits `{"close_skipped": "", "order_post_result": …}`.
Without `--execute`: emits `order_post_result.success=false, status="dry_run"` and posts nothing.

## Overrides
- `BTC5M_REPO`      — path to trading repo (default `<workspace>/pm-hl-conservative-plus-repo`)
- `BTC5M_ENV_FILE`  — path to `.env` (default `$BTC5M_REPO/.env`)
- `BTC5M_RUNNER`    — path to runner script
## Your Builder Profile & Attribution (added 2026-06-20)
From your profile:
- Builder: bank
- Address (API only): 0x2302703692dfb6d4f7c02cc32a36a6e75cada4d2 (used as PM_FUNDER)
- Builder code: 0x368ef0366e3ab1b362d8ce9256c11bf88e17aa50c590447c31003bac8c08df58 (used for order attribution on all live orders)
- L2 creds integrated (PM_API_KEY etc from your paste; .env set to derive L2 from PM_PRIVATE_KEY for consistency to avoid 401)

The pm_live_trade_runner.py now passes builder_code in OrderArgs for both OPEN and CLOSE.

If you rotate keys, update .env and re-test dry run.

To use a specific L2 set instead of derive, fill the three PM_API_* lines in .env (must match the PM_PRIVATE_KEY signer + funder + sig=1).

