from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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


def _json(value):
    return json.dumps(value, separators=(",", ":"))


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
    positions = list_local_positions_marked(db_path)
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


def _find_source_price_map(db_path: Path | str = DB_PATH) -> dict[tuple[str, str], float]:
    prices = {}
    for row in list_source_positions(500, db_path):
        key = (row.get("market_slug") or "", row.get("outcome") or "")
        try:
            prices[key] = float(row.get("price") or 0.0)
        except (TypeError, ValueError):
            prices[key] = 0.0
    return prices


def list_local_positions_marked(db_path: Path | str = DB_PATH) -> list[dict]:
    positions = get_local_positions(db_path)
    price_map = _find_source_price_map(db_path)
    marked = []
    for row in positions:
        current_price = price_map.get((row.get("market_slug") or "", row.get("outcome") or ""))
        if current_price is None or current_price <= 0:
            current_price = float(row.get("avg_price") or 0.0)
        shares = float(row.get("shares") or 0.0)
        avg_price = float(row.get("avg_price") or 0.0)
        market_value = round(shares * current_price, 2)
        cost_basis = round(shares * avg_price, 2)
        marked_row = dict(row)
        marked_row.update(
            {
                "current_price": round(current_price, 4),
                "market_value": market_value,
                "cost_basis": cost_basis,
                "unrealized_pnl": round(market_value - cost_basis, 2),
            }
        )
        marked.append(marked_row)
    marked.sort(key=lambda row: (row["market_value"], row["updated_at"]), reverse=True)
    return marked


def refresh_local_position_market_values(db_path: Path | str = DB_PATH) -> int:
    positions = list_local_positions_marked(db_path)
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


def trade_analytics(db_path: Path | str = DB_PATH) -> dict:
    orders = list_all_copy_orders(db_path)
    price_map = _find_source_price_map(db_path)
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

    for lots in lots_by_key.values():
        for lot in lots:
            if lot["remaining_shares"] <= 1e-9:
                continue
            current_price = price_map.get((lot["market_slug"], lot["outcome"]), lot["entry_price"])
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

    open_lots.sort(key=lambda row: row["entry_time"], reverse=True)
    closed_trades.sort(key=lambda row: row["exit_time"], reverse=True)
    daily_rows = [
        {"date": day, "realized_pnl": pnl}
        for day, pnl in sorted(daily_realized.items(), reverse=True)
    ]
    return {
        "open_trades": open_lots,
        "closed_trades": closed_trades,
        "daily_realized": daily_rows,
    }


def daily_portfolio_performance(db_path: Path | str = DB_PATH) -> list[dict]:
    snapshots = list_portfolio_snapshots(db_path, limit=1000)
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
        for table in ("source_trades", "source_positions", "copy_orders", "local_positions", "portfolio_snapshots", "sync_runs", "logs"):
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
    snapshot_portfolio(starting_balance, 0.0, starting_balance, 0, db_path)
    log(
        "INFO",
        "reset",
        "Runtime state reset for fresh copy session.",
        {"starting_balance": starting_balance, "copy_start_at": copy_start_at, "leader_wallet_address": leader_wallet_address},
        db_path,
    )
