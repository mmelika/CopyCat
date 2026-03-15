from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "copytrader.db"

DEFAULT_SETTINGS = {
    "target_handle": "GamblingIsAllYouNeed",
    "target_wallet": "",
    "leader_wallet_address": "0xBEbe49168Cc3FE33FD9060c6fc83355c3359A8B5",
    "execution_mode": "paper",
    "live_wallet_address": "",
    "live_api_base_url": "https://clob.polymarket.com",
    "live_trading_enabled": 0,
    "live_max_order_usd": 25.0,
    "live_price_buffer_bps": 20,
    "sync_interval_ms": 1200,
    "trade_fetch_limit": 10,
    "max_total_exposure_usd": 100.0,
    "paper_starting_balance": 100.0,
    "paper_cash_balance": 100.0,
    "slippage_bps": 30,
    "shadow_extra_slippage_bps": 15,
    "copy_sells": 1,
    "auto_run": 1,
}

DEFAULT_APP_STATE = {
    "engine_status": "RUNNING",
    "copy_start_at": "",
    "last_sync_at": "",
    "last_sync_message": "Waiting for first sync.",
    "last_error": "",
    "resolved_target_wallet": "",
    "leader_wallet_value": "0",
    "leader_wallet_updated_at": "",
    "bootstrap_positions_done_at": "",
    "live_last_intent_at": "",
    "live_last_intent_status": "",
    "live_last_intent_error": "",
}

API_TIMEOUT_SECONDS = 4
USER_AGENT = "CopyPelosi/0.1"
