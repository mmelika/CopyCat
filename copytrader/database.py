from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH, DEFAULT_APP_STATE, DEFAULT_SETTINGS


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value):
    return json.dumps(value, separators=(",", ":"))


@contextmanager
def connect(db_path: Path | str = DB_PATH):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str = DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_trades (
                source_trade_id TEXT PRIMARY KEY,
                source_handle TEXT,
                source_wallet TEXT,
                market_slug TEXT,
                market_title TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                shares REAL,
                amount_usd REAL,
                created_at TEXT,
                status TEXT,
                copy_status TEXT NOT NULL DEFAULT 'pending',
                copied_order_id TEXT,
                last_error TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_positions (
                position_key TEXT PRIMARY KEY,
                market_slug TEXT,
                market_title TEXT,
                outcome TEXT,
                side TEXT,
                price REAL,
                shares REAL,
                notional_usd REAL,
                updated_at TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS copy_orders (
                order_id TEXT PRIMARY KEY,
                source_trade_id TEXT,
                market_slug TEXT,
                market_title TEXT,
                outcome TEXT,
                side TEXT,
                requested_amount_usd REAL,
                executed_price REAL,
                shares REAL,
                status TEXT,
                failure_reason TEXT,
                created_at TEXT,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS local_positions (
                position_key TEXT PRIMARY KEY,
                market_slug TEXT,
                market_title TEXT,
                outcome TEXT,
                side TEXT,
                shares REAL NOT NULL,
                avg_price REAL NOT NULL,
                notional_usd REAL NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                cash_balance REAL NOT NULL,
                gross_exposure REAL NOT NULL,
                net_value REAL NOT NULL,
                positions_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                trades_seen INTEGER NOT NULL DEFAULT 0,
                new_trades INTEGER NOT NULL DEFAULT 0,
                copied INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                component TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT
            );
            """
        )

    seed_defaults(db_path)


def seed_defaults(db_path: Path | str = DB_PATH) -> None:
    now = utc_now()
    with connect(db_path) as conn:
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                (key, str(value), now),
            )
        for key, value in DEFAULT_APP_STATE.items():
            conn.execute(
                "INSERT OR IGNORE INTO app_state(key, value, updated_at) VALUES (?, ?, ?)",
                (key, str(value), now),
            )
    if not list_portfolio_snapshots(db_path, 1):
        settings = get_settings(db_path)
        snapshot_portfolio(
            settings["paper_cash_balance"],
            0.0,
            settings["paper_cash_balance"],
            0,
            db_path,
        )


def _coerce_setting(key: str, value: str):
    default = DEFAULT_SETTINGS.get(key)
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    return value


def get_settings(db_path: Path | str = DB_PATH) -> dict:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: _coerce_setting(row["key"], row["value"]) for row in rows}


def update_settings(values: dict, db_path: Path | str = DB_PATH) -> None:
    now = utc_now()
    with connect(db_path) as conn:
        for key, value in values.items():
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, str(value), now),
            )


def get_app_state(db_path: Path | str = DB_PATH) -> dict:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM app_state").fetchall()
    data = DEFAULT_APP_STATE.copy()
    data.update({row["key"]: row["value"] for row in rows})
    return data


def set_app_state(key: str, value, db_path: Path | str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_state(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str(value), utc_now()),
        )


def log(level: str, component: str, message: str, details=None, db_path: Path | str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO logs(ts, level, component, message, details) VALUES (?, ?, ?, ?, ?)",
            (utc_now(), level, component, message, _json(details) if details is not None else None),
        )


def list_logs(limit: int = 50, db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_sync_run(status: str = "RUNNING", db_path: Path | str = DB_PATH) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO sync_runs(started_at, status) VALUES (?, ?)",
            (utc_now(), status),
        )
        return cur.lastrowid


def finish_sync_run(
    run_id: int,
    *,
    status: str,
    trades_seen: int,
    new_trades: int,
    copied: int,
    failed: int,
    latency_ms: int,
    message: str,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE sync_runs
            SET finished_at=?, status=?, trades_seen=?, new_trades=?, copied=?, failed=?, latency_ms=?, message=?
            WHERE id=?
            """,
            (utc_now(), status, trades_seen, new_trades, copied, failed, latency_ms, message, run_id),
        )


def list_sync_runs(limit: int = 20, db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_source_positions(positions: list[dict], db_path: Path | str = DB_PATH) -> None:
    now = utc_now()
    with connect(db_path) as conn:
        for position in positions:
            conn.execute(
                """
                INSERT INTO source_positions(
                    position_key, market_slug, market_title, outcome, side, price, shares, notional_usd, updated_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_key) DO UPDATE SET
                    market_slug=excluded.market_slug,
                    market_title=excluded.market_title,
                    outcome=excluded.outcome,
                    side=excluded.side,
                    price=excluded.price,
                    shares=excluded.shares,
                    notional_usd=excluded.notional_usd,
                    updated_at=excluded.updated_at,
                    raw_json=excluded.raw_json
                """,
                (
                    position["position_key"],
                    position.get("market_slug"),
                    position.get("market_title"),
                    position.get("outcome"),
                    position.get("side"),
                    position.get("price"),
                    position.get("shares"),
                    position.get("notional_usd"),
                    position.get("updated_at") or now,
                    _json(position),
                ),
            )


def list_source_positions(limit: int = 100, db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM source_positions
            ORDER BY notional_usd DESC, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_source_trades(trades: list[dict], db_path: Path | str = DB_PATH) -> int:
    new_count = 0
    with connect(db_path) as conn:
        for trade in trades:
            existing = conn.execute(
                "SELECT 1 FROM source_trades WHERE source_trade_id=?",
                (trade["source_trade_id"],),
            ).fetchone()
            if existing is None:
                new_count += 1
            conn.execute(
                """
                INSERT INTO source_trades(
                    source_trade_id, source_handle, source_wallet, market_slug, market_title, outcome,
                    side, price, shares, amount_usd, created_at, status, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_trade_id) DO UPDATE SET
                    market_slug=excluded.market_slug,
                    market_title=excluded.market_title,
                    outcome=excluded.outcome,
                    side=excluded.side,
                    price=excluded.price,
                    shares=excluded.shares,
                    amount_usd=excluded.amount_usd,
                    created_at=excluded.created_at,
                    status=excluded.status,
                    raw_json=excluded.raw_json
                """,
                (
                    trade["source_trade_id"],
                    trade.get("source_handle"),
                    trade.get("source_wallet"),
                    trade.get("market_slug"),
                    trade.get("market_title"),
                    trade.get("outcome"),
                    trade.get("side"),
                    trade.get("price"),
                    trade.get("shares"),
                    trade.get("amount_usd"),
                    trade.get("created_at"),
                    trade.get("status"),
                    _json(trade),
                ),
            )
    return new_count


def get_existing_source_trade_ids(trade_ids: list[str], db_path: Path | str = DB_PATH) -> set[str]:
    if not trade_ids:
        return set()
    placeholders = ",".join("?" for _ in trade_ids)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT source_trade_id FROM source_trades WHERE source_trade_id IN ({placeholders})",
            trade_ids,
        ).fetchall()
    return {row["source_trade_id"] for row in rows}


def list_source_trades(limit: int = 100, db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM source_trades ORDER BY datetime(created_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_pending_source_trades(db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM source_trades
            WHERE copy_status='pending'
            ORDER BY datetime(created_at) ASC, source_trade_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def mark_source_trade(source_trade_id: str, copy_status: str, copied_order_id=None, last_error=None, db_path: Path | str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE source_trades
            SET copy_status=?, copied_order_id=?, last_error=?
            WHERE source_trade_id=?
            """,
            (copy_status, copied_order_id, last_error, source_trade_id),
        )


def insert_copy_order(order: dict, db_path: Path | str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO copy_orders(
                order_id, source_trade_id, market_slug, market_title, outcome, side,
                requested_amount_usd, executed_price, shares, status, failure_reason, created_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["order_id"],
                order.get("source_trade_id"),
                order.get("market_slug"),
                order.get("market_title"),
                order.get("outcome"),
                order.get("side"),
                order.get("requested_amount_usd"),
                order.get("executed_price"),
                order.get("shares"),
                order.get("status"),
                order.get("failure_reason"),
                order.get("created_at"),
                _json(order),
            ),
        )


def list_copy_orders(limit: int = 100, db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM copy_orders ORDER BY datetime(created_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_local_positions(db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM local_positions
            WHERE ABS(shares) > 0.0000001
            ORDER BY notional_usd DESC, updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_local_position(position_key: str, db_path: Path | str = DB_PATH) -> dict | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM local_positions WHERE position_key=?",
            (position_key,),
        ).fetchone()
    return dict(row) if row else None


def update_local_position(position_key: str, values: dict, db_path: Path | str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO local_positions(
                position_key, market_slug, market_title, outcome, side, shares, avg_price, notional_usd, realized_pnl, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_key) DO UPDATE SET
                market_slug=excluded.market_slug,
                market_title=excluded.market_title,
                outcome=excluded.outcome,
                side=excluded.side,
                shares=excluded.shares,
                avg_price=excluded.avg_price,
                notional_usd=excluded.notional_usd,
                realized_pnl=excluded.realized_pnl,
                updated_at=excluded.updated_at
            """,
            (
                position_key,
                values.get("market_slug"),
                values.get("market_title"),
                values.get("outcome"),
                values.get("side"),
                values.get("shares"),
                values.get("avg_price"),
                values.get("notional_usd"),
                values.get("realized_pnl", 0.0),
                values.get("updated_at", utc_now()),
            ),
        )


def snapshot_portfolio(
    cash_balance: float,
    gross_exposure: float,
    net_value: float,
    positions_count: int,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO portfolio_snapshots(ts, cash_balance, gross_exposure, net_value, positions_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (utc_now(), cash_balance, gross_exposure, net_value, positions_count),
        )


def list_portfolio_snapshots(db_path: Path | str = DB_PATH, limit: int = 120) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM portfolio_snapshots
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def portfolio_totals(db_path: Path | str = DB_PATH) -> dict:
    settings = get_settings(db_path)
    positions = get_local_positions(db_path)
    gross_exposure = round(sum(row["notional_usd"] for row in positions), 2)
    cash_balance = float(settings["paper_cash_balance"])
    realized_pnl = round(sum(row["realized_pnl"] for row in positions), 2)
    net_value = round(cash_balance + gross_exposure, 2)
    return {
        "cash_balance": cash_balance,
        "gross_exposure": gross_exposure,
        "net_value": net_value,
        "positions_count": len(positions),
        "realized_pnl": realized_pnl,
    }


def reset_runtime_state(
    *,
    starting_balance: float,
    copy_start_at: str,
    leader_wallet_address: str,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        for table in ("source_trades", "source_positions", "copy_orders", "local_positions", "portfolio_snapshots", "sync_runs", "logs"):
            conn.execute(f"DELETE FROM {table}")
    update_settings(
        {
            "paper_starting_balance": round(float(starting_balance), 2),
            "paper_cash_balance": round(float(starting_balance), 2),
            "max_copy_trade_usd": round(float(starting_balance), 2),
            "max_total_exposure_usd": round(float(starting_balance), 2),
            "leader_wallet_address": leader_wallet_address,
        },
        db_path,
    )
    set_app_state("copy_start_at", copy_start_at, db_path)
    set_app_state("last_sync_at", "", db_path)
    set_app_state("last_sync_message", f"Fresh start from {copy_start_at}. Waiting for new source trades.", db_path)
    set_app_state("last_error", "", db_path)
    set_app_state("leader_wallet_value", "0", db_path)
    set_app_state("leader_wallet_updated_at", "", db_path)
    snapshot_portfolio(starting_balance, 0.0, starting_balance, 0, db_path)
    log(
        "INFO",
        "reset",
        "Runtime state reset for fresh copy session.",
        {"starting_balance": starting_balance, "copy_start_at": copy_start_at, "leader_wallet_address": leader_wallet_address},
        db_path,
    )
