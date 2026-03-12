from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import DB_PATH, DEFAULT_APP_STATE, DEFAULT_SETTINGS

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def pacific_day(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(PACIFIC_TZ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _parse_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json(value):
    return json.dumps(value, separators=(",", ":"))


def _normalize_text(value: str) -> str:
    text = (value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _record_payload(record: dict) -> dict:
    raw_json = record.get("raw_json")
    if not raw_json:
        return {}
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _position_aliases(record: dict) -> set[str]:
    payload = _record_payload(record)
    outcome = _normalize_text(record.get("outcome") or payload.get("outcome") or payload.get("outcomeName") or "")
    aliases = set()
    position_key = (record.get("position_key") or "").strip()
    if position_key:
        aliases.add(f"pk:{position_key}")
    market_slug = (record.get("market_slug") or payload.get("market_slug") or payload.get("marketSlug") or payload.get("slug") or "").strip().lower()
    if market_slug and outcome:
        aliases.add(f"slug:{market_slug}|{outcome}")
    market_title = _normalize_text(record.get("market_title") or payload.get("market_title") or payload.get("marketTitle") or payload.get("title") or "")
    if market_title and outcome:
        aliases.add(f"title:{market_title}|{outcome}")
    return aliases


@contextmanager
def connect(db_path: Path | str = DB_PATH):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
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

            CREATE TABLE IF NOT EXISTS shadow_orders (
                shadow_order_id TEXT PRIMARY KEY,
                source_trade_id TEXT,
                market_slug TEXT,
                market_title TEXT,
                outcome TEXT,
                side TEXT,
                requested_amount_usd REAL,
                reference_price REAL,
                paper_price REAL,
                estimated_live_price REAL,
                estimated_live_shares REAL,
                price_delta_bps REAL,
                status TEXT,
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

            CREATE TABLE IF NOT EXISTS shadow_portfolio_snapshots (
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
    if not list_shadow_portfolio_snapshots(db_path, 1):
        settings = get_settings(db_path)
        snapshot_shadow_portfolio(
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


def replace_source_positions(positions: list[dict], db_path: Path | str = DB_PATH) -> None:
    current_keys = {position["position_key"] for position in positions if position.get("position_key")}
    with connect(db_path) as conn:
        if current_keys:
            placeholders = ",".join("?" for _ in current_keys)
            conn.execute(f"DELETE FROM source_positions WHERE position_key NOT IN ({placeholders})", tuple(current_keys))
        else:
            conn.execute("DELETE FROM source_positions")
    upsert_source_positions(positions, db_path)


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


def insert_shadow_order(order: dict, db_path: Path | str = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO shadow_orders(
                shadow_order_id, source_trade_id, market_slug, market_title, outcome, side,
                requested_amount_usd, reference_price, paper_price, estimated_live_price,
                estimated_live_shares, price_delta_bps, status, created_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["shadow_order_id"],
                order.get("source_trade_id"),
                order.get("market_slug"),
                order.get("market_title"),
                order.get("outcome"),
                order.get("side"),
                order.get("requested_amount_usd"),
                order.get("reference_price"),
                order.get("paper_price"),
                order.get("estimated_live_price"),
                order.get("estimated_live_shares"),
                order.get("price_delta_bps"),
                order.get("status"),
                order.get("created_at"),
                _json(order),
            ),
        )


def list_shadow_orders(limit: int = 100, db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM shadow_orders ORDER BY datetime(created_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def shadow_order_summary(db_path: Path | str = DB_PATH) -> dict:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                AVG(ABS(price_delta_bps)) AS avg_abs_price_delta_bps,
                MAX(ABS(price_delta_bps)) AS max_abs_price_delta_bps
            FROM shadow_orders
            """
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "avg_abs_price_delta_bps": round(float(row["avg_abs_price_delta_bps"] or 0.0), 2),
        "max_abs_price_delta_bps": round(float(row["max_abs_price_delta_bps"] or 0.0), 2),
    }


def list_sell_match_audit(limit: int = 20, db_path: Path | str = DB_PATH) -> list[dict]:
    rows = list_copy_orders(limit * 4, db_path)
    audit_rows = []
    for row in rows:
        if row.get("side") != "SELL":
            continue
        payload = {}
        try:
            payload = json.loads(row.get("raw_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        audit_rows.append(
            {
                "created_at": row.get("created_at"),
                "market_title": row.get("market_title"),
                "outcome": row.get("outcome"),
                "requested_amount_usd": row.get("requested_amount_usd"),
                "executed_price": row.get("executed_price"),
                "position_key": payload.get("position_key") or "",
                "match_strategy": payload.get("match_strategy") or ("manual" if not row.get("source_trade_id") else "unlabeled"),
                "source_trade_id": row.get("source_trade_id") or "",
            }
        )
        if len(audit_rows) >= limit:
            break
    return audit_rows


def list_all_copy_orders(db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM copy_orders
            WHERE status='FILLED'
            ORDER BY datetime(created_at) ASC, order_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_all_shadow_orders(db_path: Path | str = DB_PATH) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM shadow_orders
            WHERE status='SHADOW'
            ORDER BY datetime(created_at) ASC, shadow_order_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_latest_copy_order_for_position(position_key: str, db_path: Path | str = DB_PATH) -> dict | None:
    market_slug, _, outcome = position_key.partition(":")
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM copy_orders
            WHERE status='FILLED' AND market_slug=? AND outcome=?
            ORDER BY datetime(created_at) DESC, order_id DESC
            LIMIT 1
            """,
            (market_slug, outcome),
        ).fetchone()
    return dict(row) if row else None


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


def snapshot_shadow_portfolio(
    cash_balance: float,
    gross_exposure: float,
    net_value: float,
    positions_count: int,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO shadow_portfolio_snapshots(ts, cash_balance, gross_exposure, net_value, positions_count)
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


def list_shadow_portfolio_snapshots(db_path: Path | str = DB_PATH, limit: int = 120) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM shadow_portfolio_snapshots
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def portfolio_totals(db_path: Path | str = DB_PATH, source_positions: list[dict] | None = None) -> dict:
    settings = get_settings(db_path)
    analytics = trade_analytics(db_path, source_positions=source_positions)
    positions = analytics["open_positions"]
    gross_exposure = round(sum(row["market_value"] for row in positions), 2)
    cash_balance = float(settings["paper_cash_balance"])
    realized_pnl = round(sum(row["realized_pnl"] for row in positions), 2)
    net_value = round(cash_balance + gross_exposure, 2)
    starting_balance = float(settings["paper_starting_balance"])
    total_gain = round(net_value - starting_balance, 2)
    total_gain_pct = round((total_gain / starting_balance) * 100, 2) if starting_balance else 0.0
    return {
        "cash_balance": cash_balance,
        "gross_exposure": gross_exposure,
        "net_value": net_value,
        "positions_count": len(positions),
        "realized_pnl": realized_pnl,
        "starting_balance": starting_balance,
        "total_gain": total_gain,
        "total_gain_pct": total_gain_pct,
    }


def shadow_portfolio_totals(db_path: Path | str = DB_PATH, source_positions: list[dict] | None = None) -> dict:
    settings = get_settings(db_path)
    analytics = shadow_trade_analytics(db_path, source_positions=source_positions)
    positions = analytics["open_positions"]
    gross_exposure = round(sum(float(row["market_value"]) for row in positions), 2)
    starting_balance = float(settings["paper_starting_balance"])
    total_buy_notional = round(
        sum(float(order.get("requested_amount_usd") or 0.0) for order in list_all_shadow_orders(db_path) if order.get("side") == "BUY"),
        2,
    )
    total_sell_proceeds = round(sum(float(row.get("proceeds") or 0.0) for row in analytics["closed_trades"]), 2)
    cash_balance = round(starting_balance - total_buy_notional + total_sell_proceeds, 2)
    realized_pnl = round(sum(float(row.get("pnl") or 0.0) for row in analytics["closed_trades"]), 2)
    net_value = round(cash_balance + gross_exposure, 2)
    total_gain = round(net_value - starting_balance, 2)
    total_gain_pct = round((total_gain / starting_balance) * 100, 2) if starting_balance else 0.0
    return {
        "cash_balance": cash_balance,
        "gross_exposure": gross_exposure,
        "net_value": net_value,
        "positions_count": len(positions),
        "realized_pnl": realized_pnl,
        "starting_balance": starting_balance,
        "total_gain": total_gain,
        "total_gain_pct": total_gain_pct,
    }


def _find_source_price_map(db_path: Path | str = DB_PATH) -> dict[tuple[str, str], float]:
    prices = {}
    for row in list_source_positions(500, db_path):
        key = (row.get("market_slug") or "", row.get("outcome") or "")
        try:
            prices[key] = float(row.get("price") or 0.0)
        except (TypeError, ValueError):
            prices[key] = 0.0
    return prices


def _find_source_price_alias_map(db_path: Path | str = DB_PATH) -> dict[str, float]:
    prices = {}
    for row in list_source_positions(1000, db_path):
        try:
            price = float(row.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        for alias in _position_aliases(row):
            prices[alias] = price
    return prices


def _price_maps_from_positions(source_positions: list[dict]) -> tuple[dict[tuple[str, str], float], dict[str, float]]:
    price_map: dict[tuple[str, str], float] = {}
    alias_price_map: dict[str, float] = {}
    for row in source_positions or []:
        try:
            price = float(row.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        key = (row.get("market_slug") or "", row.get("outcome") or "")
        price_map[key] = price
        if price <= 0:
            continue
        for alias in _position_aliases(row):
            alias_price_map[alias] = price
    return price_map, alias_price_map


def fetch_live_source_positions(db_path: Path | str = DB_PATH, limit: int = 200) -> list[dict]:
    from .polymarket import PolymarketClient

    settings = get_settings(db_path)
    app_state = get_app_state(db_path)
    target = app_state.get("resolved_target_wallet") or settings.get("target_wallet") or settings.get("target_handle") or ""
    if not target:
        return list_source_positions(limit, db_path)

    client = PolymarketClient()
    try:
        profile = client.resolve_target_wallet(target)
        positions = client.fetch_positions(profile["wallet"], limit=limit)
        return positions or list_source_positions(limit, db_path)
    except Exception:
        return list_source_positions(limit, db_path)


def fetch_live_market_prices(records: list[dict], db_path: Path | str = DB_PATH) -> list[dict]:
    from .polymarket import PolymarketClient

    client = PolymarketClient()
    try:
        return client.fetch_market_prices(records)
    except Exception:
        return []


def _resolve_mark_price(record: dict, price_map: dict[tuple[str, str], float], alias_price_map: dict[str, float], fallback_price: float) -> float:
    current_price = price_map.get((record.get("market_slug") or "", record.get("outcome") or ""))
    if current_price is None or current_price <= 0:
        for alias in _position_aliases(record):
            aliased_price = alias_price_map.get(alias)
            if aliased_price and aliased_price > 0:
                current_price = aliased_price
                break
    if current_price is None or current_price <= 0:
        current_price = fallback_price
    return float(current_price or 0.0)


def list_local_positions_marked(db_path: Path | str = DB_PATH, freeze_recent_seconds: int = 0, source_positions: list[dict] | None = None) -> list[dict]:
    positions = [dict(row) for row in trade_analytics(db_path, source_positions=source_positions)["open_positions"]]
    freeze_cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(int(freeze_recent_seconds), 0))
    marked = []
    for row in positions:
        shares = float(row.get("shares") or 0.0)
        avg_price = float(row.get("avg_price") or 0.0)
        updated_at = _parse_utc(row.get("updated_at") or "")
        if updated_at is not None and updated_at >= freeze_cutoff:
            current_price = avg_price
            market_value = round(shares * current_price, 2)
            unrealized_pnl = round(market_value - float(row.get("cost_basis") or 0.0), 2)
            row["current_price"] = round(current_price, 4)
            row["market_value"] = market_value
            row["unrealized_pnl"] = unrealized_pnl
        else:
            row["cost_basis"] = round(float(row.get("cost_basis") or 0.0), 2)
        marked_row = dict(row)
        marked.append(marked_row)
    marked.sort(key=lambda row: (row["market_value"], row["updated_at"]), reverse=True)
    return marked


def refresh_local_position_market_values(db_path: Path | str = DB_PATH, freeze_recent_seconds: int = 0, source_positions: list[dict] | None = None) -> int:
    positions = list_local_positions_marked(db_path, freeze_recent_seconds=freeze_recent_seconds, source_positions=source_positions)
    updated = 0
    for row in positions:
        market_value = round(float(row.get("market_value") or 0.0), 2)
        previous_value = round(float(row.get("notional_usd") or 0.0), 2)
        if abs(market_value - previous_value) < 0.01:
            continue
        update_local_position(
            row["position_key"],
            {
                "market_slug": row.get("market_slug"),
                "market_title": row.get("market_title"),
                "outcome": row.get("outcome"),
                "side": row.get("side"),
                "shares": row.get("shares"),
                "avg_price": row.get("avg_price"),
                "notional_usd": market_value,
                "realized_pnl": row.get("realized_pnl", 0.0),
                "updated_at": utc_now(),
            },
            db_path,
        )
        updated += 1
    return updated


def _trade_analytics_from_orders(
    orders: list[dict],
    *,
    source_positions: list[dict] | None,
    db_path: Path | str,
    local_positions_by_key: dict[str, dict] | None = None,
) -> dict:
    local_positions_by_key = local_positions_by_key or {}
    open_lots: list[dict] = []
    closed_trades: list[dict] = []
    daily_realized: dict[str, float] = {}

    lots_by_key: dict[str, list[dict]] = {}
    for order in orders:
        market_slug = order.get("market_slug") or ""
        outcome = order.get("outcome") or ""
        position_key = f"{market_slug}:{outcome}"
        lots = lots_by_key.setdefault(position_key, [])
        side = order.get("side")
        shares = float(order.get("shares") or 0.0)
        price = float(order.get("executed_price") or 0.0)
        ts = order.get("created_at") or ""

        if side == "BUY":
            lots.append(
                {
                    "position_key": position_key,
                    "market_slug": market_slug,
                    "market_title": order.get("market_title"),
                    "outcome": outcome,
                    "entry_time": ts,
                    "entry_price": price,
                    "shares": shares,
                    "remaining_shares": shares,
                    "cost_basis": round(shares * price, 2),
                }
            )
            continue

        sell_shares = shares
        while sell_shares > 1e-9 and lots:
            lot = lots[0]
            matched_shares = min(lot["remaining_shares"], sell_shares)
            pnl = round(matched_shares * (price - lot["entry_price"]), 2)
            closed_trades.append(
                {
                    "position_key": position_key,
                    "market_slug": market_slug,
                    "market_title": order.get("market_title") or lot.get("market_title"),
                    "outcome": outcome,
                    "entry_time": lot["entry_time"],
                    "exit_time": ts,
                    "entry_price": round(lot["entry_price"], 4),
                    "exit_price": round(price, 4),
                    "shares": round(matched_shares, 6),
                    "cost_basis": round(matched_shares * lot["entry_price"], 2),
                    "proceeds": round(matched_shares * price, 2),
                    "pnl": pnl,
                }
            )
            daily_key = pacific_day(ts)
            if daily_key:
                daily_realized[daily_key] = round(daily_realized.get(daily_key, 0.0) + pnl, 2)
            lot["remaining_shares"] = round(lot["remaining_shares"] - matched_shares, 6)
            sell_shares = round(sell_shares - matched_shares, 6)
            if lot["remaining_shares"] <= 1e-9:
                lots.pop(0)

    if source_positions is None:
        source_positions = fetch_live_market_prices(
            [
                {
                    "market_slug": lot.get("market_slug"),
                    "market_title": lot.get("market_title"),
                    "outcome": lot.get("outcome"),
                    "position_key": lot.get("position_key"),
                }
                for lots in lots_by_key.values()
                for lot in lots
                if float(lot.get("remaining_shares") or 0.0) > 1e-9
            ],
            db_path,
        )
        if not source_positions:
            source_positions = fetch_live_source_positions(db_path)
    price_map, alias_price_map = _price_maps_from_positions(source_positions)

    for lots in lots_by_key.values():
        for lot in lots:
            if lot["remaining_shares"] <= 1e-9:
                continue
            current_price = _resolve_mark_price(lot, price_map, alias_price_map, lot["entry_price"])
            market_value = round(lot["remaining_shares"] * current_price, 2)
            cost_basis = round(lot["remaining_shares"] * lot["entry_price"], 2)
            open_lots.append(
                {
                    "position_key": lot["position_key"],
                    "market_slug": lot["market_slug"],
                    "market_title": lot.get("market_title"),
                    "outcome": lot["outcome"],
                    "entry_time": lot["entry_time"],
                    "entry_price": round(lot["entry_price"], 4),
                    "current_price": round(current_price, 4),
                    "shares": round(lot["remaining_shares"], 6),
                    "cost_basis": cost_basis,
                    "market_value": market_value,
                    "unrealized_pnl": round(market_value - cost_basis, 2),
                }
            )

    aggregated_open_positions: dict[str, dict] = {}
    for row in open_lots:
        local_position = local_positions_by_key.get(row["position_key"])
        aggregate = aggregated_open_positions.setdefault(
            row["position_key"],
            {
                "position_key": row["position_key"],
                "market_slug": row["market_slug"],
                "market_title": row.get("market_title"),
                "outcome": row["outcome"],
                "entry_time": row["entry_time"],
                "current_price": row["current_price"],
                "shares": 0.0,
                "cost_basis": 0.0,
                "market_value": 0.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": float(local_position.get("realized_pnl") or 0.0) if local_position else 0.0,
                "side": "BUY",
                "updated_at": (local_position.get("updated_at") or row["entry_time"]) if local_position else row["entry_time"],
                "avg_price": float(local_position.get("avg_price") or 0.0) if local_position else 0.0,
            },
        )
        aggregate["shares"] = round(float(aggregate["shares"]) + float(row["shares"]), 6)
        aggregate["cost_basis"] = round(float(aggregate["cost_basis"]) + float(row["cost_basis"]), 2)
        aggregate["market_value"] = round(float(aggregate["market_value"]) + float(row["market_value"]), 2)
        aggregate["unrealized_pnl"] = round(float(aggregate["unrealized_pnl"]) + float(row["unrealized_pnl"]), 2)
        aggregate["entry_time"] = min(aggregate["entry_time"], row["entry_time"]) if aggregate["entry_time"] else row["entry_time"]
        aggregate["current_price"] = row["current_price"]
    for aggregate in aggregated_open_positions.values():
        local_position = local_positions_by_key.get(aggregate["position_key"])
        shares = float(aggregate["shares"] or 0.0)
        aggregate["avg_price"] = round((float(aggregate["cost_basis"]) / shares), 4) if shares > 0 else 0.0
        if local_position:
            aggregate["updated_at"] = local_position.get("updated_at") or aggregate["updated_at"]
            aggregate["realized_pnl"] = round(float(local_position.get("realized_pnl") or aggregate["realized_pnl"]), 2)

    realized_by_key: dict[str, float] = {}
    for row in closed_trades:
        realized_by_key[row["position_key"]] = round(realized_by_key.get(row["position_key"], 0.0) + float(row["pnl"]), 2)
    for position_key, aggregate in aggregated_open_positions.items():
        if position_key not in local_positions_by_key:
            aggregate["realized_pnl"] = realized_by_key.get(position_key, 0.0)

    open_lots.sort(key=lambda row: row["entry_time"], reverse=True)
    closed_trades.sort(key=lambda row: row["exit_time"], reverse=True)
    daily_rows = [
        {"date": day, "realized_pnl": pnl}
        for day, pnl in sorted(daily_realized.items(), reverse=True)
    ]
    return {
        "open_trades": open_lots,
        "open_positions": sorted(aggregated_open_positions.values(), key=lambda row: (row["market_value"], row["updated_at"]), reverse=True),
        "closed_trades": closed_trades,
        "daily_realized": daily_rows,
    }


def trade_analytics(db_path: Path | str = DB_PATH, source_positions: list[dict] | None = None) -> dict:
    return _trade_analytics_from_orders(
        list_all_copy_orders(db_path),
        source_positions=source_positions,
        db_path=db_path,
        local_positions_by_key={row["position_key"]: row for row in get_local_positions(db_path)},
    )


def shadow_trade_analytics(db_path: Path | str = DB_PATH, source_positions: list[dict] | None = None) -> dict:
    shadow_orders = [
        {
            "market_slug": row.get("market_slug"),
            "market_title": row.get("market_title"),
            "outcome": row.get("outcome"),
            "side": row.get("side"),
            "requested_amount_usd": row.get("requested_amount_usd"),
            "executed_price": row.get("estimated_live_price"),
            "shares": row.get("estimated_live_shares"),
            "created_at": row.get("created_at"),
        }
        for row in list_all_shadow_orders(db_path)
    ]
    return _trade_analytics_from_orders(
        shadow_orders,
        source_positions=source_positions,
        db_path=db_path,
        local_positions_by_key=None,
    )


def profit_verification(db_path: Path | str = DB_PATH) -> dict:
    settings = get_settings(db_path)
    portfolio = portfolio_totals(db_path)
    analytics = trade_analytics(db_path)
    orders = list_all_copy_orders(db_path)

    starting_balance = round(float(settings["paper_starting_balance"]), 2)
    total_buy_notional = round(sum(float(order.get("requested_amount_usd") or 0.0) for order in orders if order.get("side") == "BUY"), 2)
    total_sell_proceeds = round(sum(float(row.get("proceeds") or 0.0) for row in analytics["closed_trades"]), 2)
    reconstructed_cash = round(starting_balance - total_buy_notional + total_sell_proceeds, 2)
    open_market_value = round(sum(float(row.get("market_value") or 0.0) for row in analytics["open_trades"]), 2)
    reconstructed_net_value = round(reconstructed_cash + open_market_value, 2)
    closed_realized_pnl = round(sum(float(row.get("pnl") or 0.0) for row in analytics["closed_trades"]), 2)
    open_unrealized_pnl = round(sum(float(row.get("unrealized_pnl") or 0.0) for row in analytics["open_trades"]), 2)
    expected_total_gain = round(closed_realized_pnl + open_unrealized_pnl, 2)
    displayed_total_gain = round(float(portfolio["total_gain"]), 2)
    cash_difference = round(float(portfolio["cash_balance"]) - reconstructed_cash, 2)
    net_value_difference = round(float(portfolio["net_value"]) - reconstructed_net_value, 2)
    gain_difference = round(displayed_total_gain - expected_total_gain, 2)
    max_difference = max(abs(cash_difference), abs(net_value_difference), abs(gain_difference))
    verified = max_difference < 0.02

    return {
        "verified": verified,
        "max_difference": round(max_difference, 2),
        "cash_difference": cash_difference,
        "net_value_difference": net_value_difference,
        "gain_difference": gain_difference,
        "reconstructed_cash": reconstructed_cash,
        "reconstructed_net_value": reconstructed_net_value,
        "open_market_value": open_market_value,
        "closed_realized_pnl": closed_realized_pnl,
        "open_unrealized_pnl": open_unrealized_pnl,
        "expected_total_gain": expected_total_gain,
        "displayed_total_gain": displayed_total_gain,
        "total_buy_notional": total_buy_notional,
        "total_sell_proceeds": total_sell_proceeds,
        "orders_count": len(orders),
    }


def live_profit_verification(source_positions: list[dict], db_path: Path | str = DB_PATH) -> dict:
    settings = get_settings(db_path)
    orders = list_all_copy_orders(db_path)

    closed_realized_pnl = 0.0
    open_lots: list[dict] = []
    lots_by_key: dict[str, list[dict]] = {}
    total_buy_notional = 0.0
    total_sell_proceeds = 0.0

    for order in orders:
        market_slug = order.get("market_slug") or ""
        outcome = order.get("outcome") or ""
        position_key = f"{market_slug}:{outcome}"
        side = order.get("side")
        shares = float(order.get("shares") or 0.0)
        price = float(order.get("executed_price") or 0.0)
        amount = round(float(order.get("requested_amount_usd") or 0.0), 2)
        lots = lots_by_key.setdefault(position_key, [])

        if side == "BUY":
            total_buy_notional = round(total_buy_notional + amount, 2)
            lots.append(
                {
                    "position_key": position_key,
                    "market_slug": market_slug,
                    "market_title": order.get("market_title"),
                    "outcome": outcome,
                    "entry_time": order.get("created_at") or "",
                    "entry_price": price,
                    "remaining_shares": shares,
                }
            )
            continue

        sell_shares = shares
        while sell_shares > 1e-9 and lots:
            lot = lots[0]
            matched_shares = min(float(lot["remaining_shares"]), sell_shares)
            matched_proceeds = round(matched_shares * price, 2)
            total_sell_proceeds = round(total_sell_proceeds + matched_proceeds, 2)
            closed_realized_pnl = round(closed_realized_pnl + (matched_shares * (price - float(lot["entry_price"]))), 2)
            lot["remaining_shares"] = round(float(lot["remaining_shares"]) - matched_shares, 6)
            sell_shares = round(sell_shares - matched_shares, 6)
            if lot["remaining_shares"] <= 1e-9:
                lots.pop(0)

    market_price_positions = fetch_live_market_prices(
        [
            {
                "market_slug": lot.get("market_slug"),
                "market_title": lot.get("market_title"),
                "outcome": lot.get("outcome"),
                "position_key": lot.get("position_key"),
            }
            for lots in lots_by_key.values()
            for lot in lots
            if float(lot.get("remaining_shares") or 0.0) > 1e-9
        ],
        db_path,
    )
    effective_source_positions = market_price_positions or source_positions
    price_map, alias_price_map = _price_maps_from_positions(effective_source_positions)

    open_market_value = 0.0
    open_unrealized_pnl = 0.0
    reconstructed_by_key: dict[str, dict] = {}
    for lots in lots_by_key.values():
        for lot in lots:
            remaining_shares = float(lot.get("remaining_shares") or 0.0)
            if remaining_shares <= 1e-9:
                continue
            entry_price = float(lot.get("entry_price") or 0.0)
            current_price = _resolve_mark_price(lot, price_map, alias_price_map, entry_price)
            market_value = round(remaining_shares * current_price, 2)
            cost_basis = round(remaining_shares * entry_price, 2)
            unrealized = round(market_value - cost_basis, 2)
            open_market_value = round(open_market_value + market_value, 2)
            open_unrealized_pnl = round(open_unrealized_pnl + unrealized, 2)
            lot_row = {
                "position_key": lot["position_key"],
                "market_slug": lot.get("market_slug"),
                "market_title": lot.get("market_title"),
                "outcome": lot.get("outcome"),
                "entry_time": lot.get("entry_time"),
                "entry_price": round(entry_price, 4),
                "current_price": round(current_price, 4),
                "shares": round(remaining_shares, 6),
                "market_value": market_value,
                "unrealized_pnl": unrealized,
            }
            open_lots.append(lot_row)
            aggregated = reconstructed_by_key.setdefault(
                lot["position_key"],
                {
                    "position_key": lot["position_key"],
                    "market_slug": lot.get("market_slug"),
                    "market_title": lot.get("market_title"),
                    "outcome": lot.get("outcome"),
                    "shares": 0.0,
                    "entry_cost_basis": 0.0,
                    "audit_market_value": 0.0,
                    "audit_unrealized_pnl": 0.0,
                    "audit_current_price": round(current_price, 4),
                },
            )
            aggregated["shares"] = round(float(aggregated["shares"]) + remaining_shares, 6)
            aggregated["entry_cost_basis"] = round(float(aggregated["entry_cost_basis"]) + cost_basis, 2)
            aggregated["audit_market_value"] = round(float(aggregated["audit_market_value"]) + market_value, 2)
            aggregated["audit_unrealized_pnl"] = round(float(aggregated["audit_unrealized_pnl"]) + unrealized, 2)
            aggregated["audit_current_price"] = round(current_price, 4)

    starting_balance = round(float(settings["paper_starting_balance"]), 2)
    reconstructed_cash = round(starting_balance - total_buy_notional + total_sell_proceeds, 2)
    reconstructed_net_value = round(reconstructed_cash + open_market_value, 2)
    expected_total_gain = round(closed_realized_pnl + open_unrealized_pnl, 2)
    displayed = portfolio_totals(db_path)
    displayed_positions = list_local_positions_marked(db_path)
    display_difference = round(float(displayed["net_value"]) - reconstructed_net_value, 2)
    displayed_by_key = {
        row["position_key"]: {
            "displayed_shares": round(float(row.get("shares") or 0.0), 6),
            "displayed_market_value": round(float(row.get("market_value") or 0.0), 2),
            "displayed_current_price": round(float(row.get("current_price") or 0.0), 4),
            "displayed_unrealized_pnl": round(float(row.get("unrealized_pnl") or 0.0), 2),
        }
        for row in displayed_positions
    }
    mismatch_rows = []
    for position_key in sorted(set(displayed_by_key) | set(reconstructed_by_key)):
        displayed_row = displayed_by_key.get(position_key, {})
        audit_row = reconstructed_by_key.get(position_key, {})
        displayed_value = round(float(displayed_row.get("displayed_market_value") or 0.0), 2)
        audit_value = round(float(audit_row.get("audit_market_value") or 0.0), 2)
        value_difference = round(displayed_value - audit_value, 2)
        if abs(value_difference) < 0.01:
            continue
        mismatch_rows.append(
            {
                "position_key": position_key,
                "market_slug": audit_row.get("market_slug") or position_key.split(":", 1)[0],
                "market_title": audit_row.get("market_title") or "",
                "outcome": audit_row.get("outcome") or position_key.split(":", 1)[1] if ":" in position_key else "",
                "displayed_market_value": displayed_value,
                "audit_market_value": audit_value,
                "difference": value_difference,
                "displayed_current_price": round(float(displayed_row.get("displayed_current_price") or 0.0), 4),
                "audit_current_price": round(float(audit_row.get("audit_current_price") or 0.0), 4),
                "displayed_shares": round(float(displayed_row.get("displayed_shares") or 0.0), 6),
                "audit_shares": round(float(audit_row.get("shares") or 0.0), 6),
            }
        )
    mismatch_rows.sort(key=lambda row: abs(float(row["difference"])), reverse=True)

    return {
        "verified": abs(display_difference) < 0.02,
        "displayed_net_value": round(float(displayed["net_value"]), 2),
        "displayed_cash_balance": round(float(displayed["cash_balance"]), 2),
        "displayed_marked_positions": round(float(displayed["gross_exposure"]), 2),
        "reconstructed_cash": reconstructed_cash,
        "reconstructed_net_value": reconstructed_net_value,
        "open_market_value": open_market_value,
        "closed_realized_pnl": round(closed_realized_pnl, 2),
        "open_unrealized_pnl": round(open_unrealized_pnl, 2),
        "expected_total_gain": expected_total_gain,
        "display_difference": display_difference,
        "orders_count": len(orders),
        "source_positions_count": len(source_positions or []),
        "open_positions_count": len(open_lots),
        "open_positions_sample": open_lots[:10],
        "position_mismatches": mismatch_rows[:20],
    }


def daily_portfolio_performance(db_path: Path | str = DB_PATH) -> list[dict]:
    return _daily_portfolio_performance(list_portfolio_snapshots(db_path, limit=1000))


def shadow_daily_portfolio_performance(db_path: Path | str = DB_PATH) -> list[dict]:
    return _daily_portfolio_performance(list_shadow_portfolio_snapshots(db_path, limit=1000))


def _daily_portfolio_performance(snapshots: list[dict]) -> list[dict]:
    by_day: dict[str, dict] = {}
    for snapshot in snapshots:
        day = pacific_day(snapshot.get("ts") or "")
        if not day:
            continue
        by_day[day] = snapshot

    ordered_days = sorted(by_day)
    results = []
    previous_net = None
    for day in ordered_days:
        snapshot = by_day[day]
        net_value = round(float(snapshot["net_value"]), 2)
        day_change = round(net_value - previous_net, 2) if previous_net is not None else 0.0
        results.append({"date": day, "net_value": net_value, "day_change": day_change})
        previous_net = net_value
    return list(reversed(results))


def reset_runtime_state(
    *,
    starting_balance: float,
    copy_start_at: str,
    leader_wallet_address: str,
    db_path: Path | str = DB_PATH,
) -> None:
    with connect(db_path) as conn:
        for table in ("source_trades", "source_positions", "copy_orders", "shadow_orders", "local_positions", "portfolio_snapshots", "shadow_portfolio_snapshots", "sync_runs", "logs"):
            conn.execute(f"DELETE FROM {table}")
    update_settings(
        {
            "paper_starting_balance": round(float(starting_balance), 2),
            "paper_cash_balance": round(float(starting_balance), 2),
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
    set_app_state("bootstrap_positions_done_at", "", db_path)
    snapshot_portfolio(starting_balance, 0.0, starting_balance, 0, db_path)
    snapshot_shadow_portfolio(starting_balance, 0.0, starting_balance, 0, db_path)
    log(
        "INFO",
        "reset",
        "Runtime state reset for fresh copy session.",
        {"starting_balance": starting_balance, "copy_start_at": copy_start_at, "leader_wallet_address": leader_wallet_address},
        db_path,
    )
