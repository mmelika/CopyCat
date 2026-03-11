# CopyPelosi

Standalone Dash platform for paper copy-trading a single Polymarket user, defaulting to `@GamblingIsAllYouNeed`.

## What it does

- Resolves one Polymarket profile or wallet target.
- Polls the target's recent trades and positions on a short interval.
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

## Notes

- Persistence lives in [data/copytrader.db](/Users/marco/Documents/AI Shit/CopyPelosi/data/copytrader.db) after the first run.
- The current execution adapter is paper-only. The backend is split so a real account executor can replace `PaperBroker` in [copytrader/engine.py](/Users/marco/Documents/AI Shit/CopyPelosi/copytrader/engine.py).
- Public Polymarket endpoints can change. The client in [copytrader/polymarket.py](/Users/marco/Documents/AI Shit/CopyPelosi/copytrader/polymarket.py) tries multiple public API shapes and fails gracefully into logs if resolution or fetches break.
