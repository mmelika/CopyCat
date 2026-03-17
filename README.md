# CopyPelosi

Polymarket copy trading bot and monitoring dashboard for mirroring a single trader into a local shadow portfolio.

If you are searching GitHub for a `Polymarket trading bot`, `Polymarket copy bot`, or `Polymarket copy trading bot`, this repo is built for that workflow: watch one Polymarket account, size copy trades off the leader's activity, track paper performance, and audit the copy logic from a live dashboard.

## What This Bot Does

- Follows one Polymarket handle or wallet and resolves the profile automatically.
- Polls recent trades and open positions on a short sync cadence.
- Mirrors buys and sells into a local shadow account with proportional sizing.
- Stores source trades, copied orders, shadow orders, local positions, sync runs, snapshots, and logs in SQLite.
- Runs a Dash dashboard with status, charts, open positions, closed trades, target trade feed, sell-match audit tables, execution comparison, and engine logs.
- Exposes health and audit endpoints for profit verification, shadow verification, closed-trade checks, and reconciliation mismatches.

## Current Feature Set

- `Shadow copy trading`: the default mode paper-trades a Polymarket account without putting capital at risk.
- `Proportional sizing`: buy size is based on the leader account value versus your local shadow equity.
- `Correlation-aware sizing`: correlated or high-conviction buys can use more of the available shadow cash.
- `Cash-reserve aware execution`: buys are constrained by deployable cash, not the older exposure-cap logic.
- `Minimum leader buy filter`: leader buys at `$40.00` or below are ignored, so copied buys only trigger when the source trade is greater than `$40.00`.
- `Minimum follower buy size`: when a copied buy is eligible, the follower now allocates at least `$1.00` on that buy, subject to available cash and the rest of the existing guardrails.
- `Live minimum order guard`: live BUY orders are now fail-closed below `$1.00`, including bootstrap/current-position buys, so the bot will not send sub-minimum marketable orders to Polymarket.
- `Tiered position cap`: once follower equity is above `$2,000`, each position is capped at `$400`, and that cap increases by `$400` for each additional `$2,500` of equity.
- `Sell copying controls`: sells can be mirrored or ignored from settings.
- `Leader-only sells by default`: follower positions now stay open unless the leader reduces, you sell manually, or you explicitly enable autonomous exit rules.
- `No inferred position-delta sells by default`: temporary position API drops no longer fabricate sell events that can close and immediately reopen a follower trade.
- `Exact sell matching`: sell reconciliation now matches by market and outcome to avoid stale-position drift.
- `Burst-trade collapsing`: rapid fills in the same market are merged before copy logic runs.
- `Per-fill source trade IDs`: fills from the same on-chain transaction are keyed separately so one leader transaction cannot collide into a buy plus an unintended opposite-side sell.
- `Bootstrap sync`: the bot can seed local shadow inventory from the leader's current open positions on first run.
- `Optional autonomous exits`: resolved losers can be swept to zero, nearly fully priced winners can auto-exit above 98c, and growth-milestone liquidation can run only when you enable autonomous sell rules.
- `Manual controls`: pause, force sync, fresh start, liquidate-all, and per-position sell buttons are available from the dashboard.
- `Audit tooling`: `/healthz`, `/audit/profit`, `/audit/shadow`, `/audit/shadow/closed`, and `/audit/reconcile` help verify the bot's state.
- `Per-sync timing breakdown`: each sync now records stage-by-stage timings in SQLite so you can separate engine work from upstream Polymarket detection lag.
- `Faster sync hot paths`: portfolio and mark-to-market updates now use the maintained `local_positions` ledger directly instead of replaying the full order history on every sync.
- `Safer SQLite concurrency`: WAL mode is initialized once instead of on every request, which reduces lock churn when the web app and sync engine share the same database.
- `Live execution scaffold`: there is a guarded Polymarket CLOB order-intent path and live account reconciliation, but this should be treated as non-production.
- `Live entry drift tracking`: live order intents now calculate entry drift from the live limit price versus the source/reference price, and the dashboard summarizes that drag alongside shadow drift.
- `Per-position drift visibility`: the Active Holdings rows now show drift per position, including average entry drift in bps and cumulative drag in USD for that holding.
- `Per-position up/down visibility`: holdings rows now show per-position unrealized up/down in USD and percent when the app can recover cost basis from the position snapshot.
- `Live trade book from live inventory`: in live mode, the open-position trade book now renders from the latest live wallet snapshot instead of the local copied-order ledger.
- `Stepped live max order cap`: live buys respect a base `$25` max order cap, step up to `$45` once portfolio value reaches `$200`, step up to `$70` at `$300`, add another `$25` for each additional `$100` until the cap reaches `$250`, then hold that `$250` single-position cap from `$1,000` up to `$5,000` of portfolio value.
- `High-conviction buy override`: when the leader buys more than `$1,000`, the live follower now uses its full allowed live max order size instead of the smaller proportional amount.

## What This Repo Is Not

- Not a multi-wallet copy trading platform.
- Not a backtester.
- Not a finished production live-trading system.
- Not using the older exposure-cap system anymore; that setting is legacy and unused.

## Stack

- Python
- Dash + Plotly
- SQLite
- Requests
- `py-clob-client` for the guarded live-order scaffold

## Dashboard

The local UI is built for operating a Polymarket shadow trading bot in real time:

- top-bar runtime status, heartbeat, execution mode, and control buttons
- shadow portfolio breakdown and realized performance
- 1D, 1W, 1M, and all-time portfolio curves
- active holdings rows with per-position drift details plus per-position unrealized up/down
- open-position and closed-trade views with a per-position sell button on open rows
- target trade feed and copied-order history
- sell-match auditing and reconciliation visibility
- sync history and engine logs
- settings modal for target, sync cadence, slippage, shadow cash, and execution mode

## Run Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

The app listens on `0.0.0.0:8060` by default, so open [http://127.0.0.1:8060](http://127.0.0.1:8060) locally or `http://YOUR_SERVER_IP:8060` from another machine. You can override that with `HOST` and `PORT`. For reverse-proxy deployments, `APP_BASE_PATH` and `APP_ROUTES_PATH` let one instance live under a subpath such as `/shadow/`.

To run the sync worker separately instead of inside the web process:

```bash
python3 run_engine.py
```

## Configuration

Most runtime settings are editable from the dashboard:

- target Polymarket handle or wallet
- reference leader wallet for sizing
- shadow starting balance and current cash balance
- sync interval and trade fetch limit
- copy-sells on or off
- minimum copied leader buy size: source buys must be greater than `$40.00`
- minimum follower copied buy size: eligible copied buys are floored at `$1.00`
- explicit leader sells only or optional inferred position-delta sells
- leader-only sells or optional autonomous exit rules
- shadow slippage and extra shadow slippage
- execution mode: `shadow` or guarded `live` scaffold

In live mode, `live_max_order_usd` is now the floor for the live order cap, not always the final cap. The engine uses the greater of that configured value or the stepped schedule based on live portfolio value:

- below `$200`: `$25`
- at `$200`: `$45`
- at `$300`: `$70`
- each additional `$100`: add `$25` more until the cap reaches `$250`
- from `$1,000` up to `$5,000`: keep the single-position cap at `$250`
- above `$5,000`: resume increasing the cap by `$25` per additional `$100`

For leader buys above `$1,000`, the live copy path now targets that full allowed cap, subject to available cash and the rest of the existing guardrails.

Temporary live test mode is environment-based so you can keep the same wallet and keys while forcing tiny live orders:

```env
LIVE_TEST_MODE=1
LIVE_TEST_MAX_ORDER_USD=1.01
LIVE_TEST_ALLOWED_MARKET_SLUGS=
```

When `LIVE_TEST_MODE=1`, the live broker forces every submitted live order to exactly the test band: a minimum of `$1.01` and a maximum of `LIVE_TEST_MAX_ORDER_USD`, which now defaults to `$1.01`. If `LIVE_TEST_ALLOWED_MARKET_SLUGS` is set, it must be a comma-separated allowlist of market slugs and live orders for any other market are rejected before submission. This only affects live execution; the shadow engine and copy-decision math stay unchanged.

The live dashboard cash snapshot normalizes CLOB collateral balances from raw base units to display dollars. If the exchange returns a value like `9486331`, the app now interprets that as `$9.49` rather than `$9,486,331`.
If the live collateral call fails transiently but a recent successful snapshot exists, the app reuses that recent collateral balance instead of briefly overwriting the dashboard with zero.
In live mode, the copy-decision path now uses that same live collateral snapshot directly for cash sizing, so the live engine can run independently with the same decision logic as shadow mode while sourcing cash from the live Polymarket account instead of a shadow balance setting. If the live collateral refresh fails, the live engine pauses itself rather than continuing with a stale or zeroed cash input.
The live dashboard's Active Holdings panel now renders from the latest live account snapshot positions instead of the local copy-order ledger, so live holdings reflect the actual Polymarket wallet inventory the engine most recently fetched.
When the live position payload includes entry-price or cost-basis fields, the dashboard now also shows each holding's unrealized up/down in USD and percent, and the live open-position trade book uses that same live inventory snapshot.
If the live snapshot table is missing or unavailable, the dashboard falls back to the older local portfolio view instead of failing the whole page.

Defaults are seeded from [`copytrader/config.py`](copytrader/config.py).

On a small EC2 instance, the main stability levers are:

- keep the sync interval conservative; the default is `2500ms`, not sub-second polling
- avoid oversizing Gunicorn workers for Dash; the shipped EC2 unit uses `1` worker and `2` threads
- let Gunicorn recycle workers periodically with `--max-requests` to avoid long-lived worker bloat

## Operations

Health check:

```bash
curl http://127.0.0.1:8050/healthz
```

Audit endpoints:

```bash
curl http://127.0.0.1:8050/audit/profit
curl http://127.0.0.1:8050/audit/shadow
curl http://127.0.0.1:8050/audit/shadow/closed
curl http://127.0.0.1:8050/audit/reconcile
```

Backup the database:

```bash
bash scripts/backup_db.sh
```

Run a parity replay audit:

```bash
python3 scripts/parity_replay_audit.py \
  --source-db data/copytrader.db
```

To compare the hypothetical leader-sized replay against a live or shadow instance, point the script at a second DB and the relevant order tables. This is useful when a live instance had less cash or different inventory than the shadow instance, and you want to know whether the engine logic would otherwise have taken the same trades:

```bash
python3 scripts/parity_replay_audit.py \
  --source-db /tmp/shadow.db \
  --compare-db /tmp/live.db \
  --compare-tables copy_orders,live_order_attempts \
  --restrict-to-compare-source-trades \
  --since-hours 12
```

The replay injects only the minimum cash needed to mirror source trade notional 1:1 and replays the full source history so sells have the same inventory context. By default it filters compare-table failures that only say the wallet already owned the shares, because those do not indicate a copy-logic mismatch.

Persistence lives in [`data/copytrader.db`](data/copytrader.db).

To inspect where sync time is being spent, query the latest timing stages:

```bash
sqlite3 data/copytrader.db "
  SELECT stage_name, duration_ms, details
  FROM sync_run_stages
  WHERE run_id = (SELECT MAX(id) FROM sync_runs WHERE status != 'RUNNING')
  ORDER BY duration_ms DESC;
"
```

The `fresh_trade_detection_lag` row is especially important: it measures how old newly seen source trades already were when the engine first noticed them. That tells you how much delay came from Polymarket's activity/position APIs instead of this app's own sync loop.

You can also inspect the summarized timing log rows:

```bash
sqlite3 data/copytrader.db "
  SELECT ts, component, message, details
  FROM logs
  WHERE component = 'timing'
  ORDER BY id DESC
  LIMIT 5;
"
```

## Deploy

This repo includes EC2 deployment helpers for running the web UI and sync engine as separate services:

- [`deploy/bootstrap_ec2.sh`](deploy/bootstrap_ec2.sh)
- [`deploy/copycat.service`](deploy/copycat.service)
- [`deploy/copycat-engine.service`](deploy/copycat-engine.service)
- [`deploy/nginx-copycat.conf`](deploy/nginx-copycat.conf)
- [`wsgi.py`](wsgi.py)

Typical flow:

```bash
git clone https://github.com/mmelika/PolyCopy.git
cd PolyCopy
bash deploy/bootstrap_ec2.sh
```

To run a second instance on the same EC2 box, give it a different port and service names and skip the Nginx rewrite:

```bash
APP_DIR=/home/ec2-user/CopyCat \
PORT=8061 \
WEB_SERVICE_NAME=polycopy-shadow-web \
ENGINE_SERVICE_NAME=polycopy-shadow-engine \
CONFIGURE_NGINX=0 \
bash deploy/bootstrap_ec2.sh
```

That pattern lets one app stay behind Nginx on port `80` while another app runs directly on a second port such as `8061`.
The bootstrap helper rewrites both the app `PORT` environment variable and the Gunicorn bind port, so each service can actually listen on its own port.

For EC2, browse to `http://YOUR_EC2_PUBLIC_IP` after bootstrap. Nginx listens on port `80` and can proxy different app instances by path, for example the live app on `/` and a shadow app on `/shadow/`, instead of exposing raw Gunicorn ports directly.

Your AWS security group must allow:

- inbound TCP `80` for the site through Nginx
- inbound TCP `22` for SSH
- inbound TCP `8060` only if you intentionally want to bypass Nginx and hit the app directly

If the instance starts timing out on both HTTP and SSH, treat it as host pressure first, not just an Nginx problem. The common checks are:

```bash
sudo journalctl -u polycopy-web -u polycopy-engine -u nginx -n 200 --no-pager
sudo dmesg -T | tail -n 100
free -m
uptime
top
```

The most likely failure mode on a small instance is resource exhaustion or restart thrash: the web process, the sync engine, SQLite activity, and repeated outbound Polymarket requests all compete for the same CPU and memory budget.

## SEO Notes

Relevant search phrases this project genuinely fits:

- Polymarket trading bot
- Polymarket copy trading bot
- Polymarket copy bot
- Polymarket shadow trading bot
- Polymarket paper trading bot
- Polymarket trader mirror bot

## Disclaimer

This is a research and monitoring tool. Shadow mode is the primary supported path. If you experiment with the live scaffold, treat it as unfinished and review the code closely before trusting it with capital.

## Extra Documents

- [`investor_term_sheet.txt`](/Users/marco/Documents/AI Shit/CopyPelosi/investor_term_sheet.txt) is a short one-page investor term sheet covering a $100 initial bot allocation, a 50/50 profit split, up to two optional retry attempts after a loss subject to agreed model improvements, and source-code access if the first attempt is profitable.
