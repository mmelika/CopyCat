from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "copytrader.db"

DEFAULT_SETTINGS = {
    "target_handle": "GamblingIsAllYouNeed",
    "target_wallet": "",
    "sync_interval_ms": 1200,
    "trade_fetch_limit": 100,
    "copy_ratio": 1.0,
    "max_copy_trade_usd": 150.0,
    "max_total_exposure_usd": 2500.0,
    "paper_starting_balance": 5000.0,
    "paper_cash_balance": 5000.0,
    "slippage_bps": 30,
    "copy_sells": 1,
    "auto_run": 1,
}

DEFAULT_APP_STATE = {
    "engine_status": "RUNNING",
    "last_sync_at": "",
    "last_sync_message": "Waiting for first sync.",
    "last_error": "",
    "resolved_target_wallet": "",
}

API_TIMEOUT_SECONDS = 4
USER_AGENT = "CopyPelosi/0.1"

