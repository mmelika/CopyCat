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
- `Sell copying controls`: sells can be mirrored or ignored from settings.
- `Exact sell matching`: sell reconciliation now matches by market and outcome to avoid stale-position drift.
- `Burst-trade collapsing`: rapid fills in the same market are merged before copy logic runs.
- `Bootstrap sync`: the bot can seed local shadow inventory from the leader's current open positions on first run.
- `Resolved-market cleanup`: resolved losers can be swept to zero and nearly fully priced winners can auto-exit above 98c.
- `Growth milestone liquidation`: the shadow portfolio can fully liquidate after holding above each +50% portfolio milestone for a stability window.
- `Manual controls`: pause, force sync, fresh start, liquidate-all, and per-position sell buttons are available from the dashboard.
- `Audit tooling`: `/healthz`, `/audit/profit`, `/audit/shadow`, `/audit/shadow/closed`, and `/audit/reconcile` help verify the bot's state.
- `Live execution scaffold`: there is a guarded Polymarket CLOB order-intent path and live account reconciliation, but this should be treated as non-production.

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

Open [http://127.0.0.1:8050](http://127.0.0.1:8050).

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
- shadow slippage and extra shadow slippage
- execution mode: `shadow` or guarded `live` scaffold

Defaults are seeded from [`copytrader/config.py`](copytrader/config.py).

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

Persistence lives in [`data/copytrader.db`](data/copytrader.db).

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
