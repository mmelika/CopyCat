# CopyPelosi

Standalone Dash platform for paper copy-trading a single Polymarket user, defaulting to `@GamblingIsAllYouNeed`.

## What it does

- Resolves one Polymarket profile or wallet target.
- Polls the target's recent trades and positions on a short interval.
- Collapses bursty same-outcome source fills so one laddered order does not get copied as many separate buys.
- Stores source activity, sync history, copied orders, local paper positions, and logs in SQLite.
- Applies copy-trade decisioning for a local paper portfolio with exposure caps and sell controls.
- Renders a monitoring dashboard with live status, tables, charting, logs, and a settings modal.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Then open `http://127.0.0.1:8050`.

You can also run on a different local port:

```bash
PORT=8060 python3 app.py
```

## Deploy To EC2

This repo includes:

- [wsgi.py](/Users/marco/Documents/AI Shit/CopyPelosi/wsgi.py) for Gunicorn
- [run_engine.py](/Users/marco/Documents/AI Shit/CopyPelosi/run_engine.py) for the standalone sync worker
- [deploy/copycat.service](/Users/marco/Documents/AI Shit/CopyPelosi/deploy/copycat.service) for the web UI `systemd` unit
- [deploy/copycat-engine.service](/Users/marco/Documents/AI Shit/CopyPelosi/deploy/copycat-engine.service) for the sync worker `systemd` unit
- [deploy/nginx-copycat.conf](/Users/marco/Documents/AI Shit/CopyPelosi/deploy/nginx-copycat.conf) for Nginx
- [deploy/bootstrap_ec2.sh](/Users/marco/Documents/AI Shit/CopyPelosi/deploy/bootstrap_ec2.sh) to install and start the app on Ubuntu EC2

Typical EC2 flow:

```bash
git clone https://github.com/mmelika/CopyCat.git
cd CopyCat
bash deploy/bootstrap_ec2.sh
```

If you want me to do the EC2 deployment directly, I need the instance IP or DNS name, the SSH username, and a usable SSH key path or access method.

## Operations

Health check:

```bash
curl http://127.0.0.1:8060/healthz
```

Database backup:

```bash
bash scripts/backup_db.sh
```

Suggested daily cron on the server:

```bash
0 3 * * * /home/ec2-user/CopyCat/scripts/backup_db.sh >> /home/ec2-user/CopyCat/backups/backup.log 2>&1
```

## Notes

- Persistence lives in [data/copytrader.db](/Users/marco/Documents/AI Shit/CopyPelosi/data/copytrader.db) after the first run.
- On EC2, run the web UI and sync engine as separate services. The web app should not own the long-lived poller.
- The current execution adapter is paper-only. The backend is split so a real account executor can replace `PaperBroker` in [copytrader/engine.py](/Users/marco/Documents/AI Shit/CopyPelosi/copytrader/engine.py).
- Public Polymarket endpoints can change. The client in [copytrader/polymarket.py](/Users/marco/Documents/AI Shit/CopyPelosi/copytrader/polymarket.py) tries multiple public API shapes and fails gracefully into logs if resolution or fetches break.
