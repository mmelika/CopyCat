#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copytrader import database
from copytrader.engine import (
    CopyTradingEngine,
    PaperBroker,
    _buy_expiry_guard_reason,
    _clamp,
    _parse_iso,
)


IGNORED_ALREADY_OWNED_TERMS = (
    "already own",
    "already owned",
    "already have",
    "position already",
    "existing position",
    "duplicate position",
)


def _load_rows(db_path: Path, query: str) -> list[dict]:
    with database.connect(db_path) as conn:
        return [dict(row) for row in conn.execute(query).fetchall()]


def _normalize_actual_rows(db_path: Path, tables: list[str], ignore_already_owned_failures: bool) -> list[dict]:
    normalized: list[dict] = []
    for table in tables:
        if table == "copy_orders":
            rows = _load_rows(
                db_path,
                """
                SELECT source_trade_id, market_slug, outcome, side, status, failure_reason,
                       requested_amount_usd, shares, executed_price, created_at
                FROM copy_orders
                ORDER BY datetime(created_at) ASC
                """,
            )
        elif table == "live_order_attempts":
            rows = _load_rows(
                db_path,
                """
                SELECT source_trade_id, market_slug, outcome, side, status, failure_reason,
                       requested_amount_usd, estimated_shares AS shares, limit_price AS executed_price, created_at
                FROM live_order_attempts
                ORDER BY datetime(created_at) ASC
                """,
            )
        elif table == "shadow_orders":
            rows = _load_rows(
                db_path,
                """
                SELECT source_trade_id, market_slug, outcome, side, status, '' AS failure_reason,
                       requested_amount_usd, estimated_live_shares AS shares, estimated_live_price AS executed_price, created_at
                FROM shadow_orders
                ORDER BY datetime(created_at) ASC
                """,
            )
        else:
            raise ValueError(f"Unsupported compare table: {table}")

        for row in rows:
            failure_reason = (row.get("failure_reason") or "").strip()
            if ignore_already_owned_failures and any(term in failure_reason.lower() for term in IGNORED_ALREADY_OWNED_TERMS):
                continue
            row["compare_table"] = table
            normalized.append(row)
    return normalized


def _ensure_cash(db_path: Path, target_cash: float) -> float:
    settings = database.get_settings(db_path)
    current_cash = round(float(settings.get("paper_cash_balance") or 0.0), 2)
    if current_cash >= target_cash - 1e-9:
        return 0.0
    injection = round(target_cash - current_cash, 2)
    database.credit_paper_cash(injection, db_path)
    starting_balance = round(float(settings.get("paper_starting_balance") or 0.0) + injection, 2)
    database.update_settings({"paper_starting_balance": starting_balance}, db_path)
    return injection


def _make_fill(source_trade: dict, requested_amount_usd: float, settings: dict, *, position_key: str = "", match_strategy: str = "") -> dict:
    side = source_trade["side"]
    source_price = max(float(source_trade.get("price") or 0.0), 0.01)
    slippage = float(settings["slippage_bps"]) / 10000.0
    executed_price = source_price * (1 + slippage) if side == "BUY" else source_price * (1 - slippage)
    executed_price = _clamp(executed_price, 0.01, 0.99)
    shares = round(float(requested_amount_usd or 0.0) / executed_price, 6) if executed_price > 0 else 0.0
    return {
        "order_id": str(uuid.uuid4()),
        "source_trade_id": source_trade["source_trade_id"],
        "market_slug": source_trade["market_slug"],
        "market_title": source_trade.get("market_title"),
        "outcome": source_trade["outcome"],
        "side": side,
        "requested_amount_usd": round(float(requested_amount_usd or 0.0), 2),
        "executed_price": round(executed_price, 4),
        "shares": shares,
        "status": "FILLED",
        "failure_reason": None,
        "created_at": source_trade.get("created_at") or database.utc_now(),
        "position_key": position_key,
        "match_strategy": match_strategy,
    }


def _simulate_parity(
    source_db: Path,
    *,
    compare_db: Path | None,
    compare_tables: list[str],
    restrict_to_compare_source_trades: bool,
    ignore_already_owned_failures: bool,
    since_hours: float | None,
) -> dict:
    source_trades = _load_rows(
        source_db,
        """
        SELECT *
        FROM source_trades
        ORDER BY datetime(created_at) ASC, source_trade_id ASC
        """,
    )
    source_settings = database.get_settings(source_db)
    compare_source_ids: set[str] = set()
    actual_rows: list[dict] = []
    if compare_db:
        actual_rows = _normalize_actual_rows(compare_db, compare_tables, ignore_already_owned_failures)
        compare_source_ids = {
            row["source_trade_id"]
            for row in _load_rows(compare_db, "SELECT source_trade_id FROM source_trades")
            if row.get("source_trade_id")
        }
    cutoff = None
    if since_hours is not None and since_hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    with tempfile.TemporaryDirectory(prefix="parity-replay-") as tmpdir:
        temp_db = Path(tmpdir) / "parity.db"
        database.init_db(temp_db)
        seed_settings = {
            "slippage_bps": source_settings["slippage_bps"],
            "copy_sells": source_settings["copy_sells"],
            "paper_starting_balance": 0.0,
            "paper_cash_balance": 0.0,
        }
        database.update_settings(seed_settings, temp_db)

        engine = CopyTradingEngine(temp_db)
        broker = PaperBroker()
        simulated_rows: list[dict] = []
        skipped_rows: list[dict] = []
        total_injected_cash = 0.0

        for trade in source_trades:
            trade = dict(trade)
            trade["position_key"] = trade.get("position_key") or f"{trade.get('market_slug')}:{trade.get('outcome')}"
            trade_time = _parse_iso(trade.get("created_at") or "")
            evaluate_trade = not restrict_to_compare_source_trades or not compare_db or trade["source_trade_id"] in compare_source_ids
            if cutoff is not None:
                evaluate_trade = evaluate_trade and bool(trade_time and trade_time >= cutoff)

            if trade["side"] == "BUY":
                expiry_reason = _buy_expiry_guard_reason(trade, now=trade_time)
                if expiry_reason:
                    if evaluate_trade:
                        skipped_rows.append(
                            {
                                "source_trade_id": trade["source_trade_id"],
                                "market_slug": trade["market_slug"],
                                "outcome": trade["outcome"],
                                "side": trade["side"],
                                "reason": expiry_reason,
                            }
                        )
                    continue
                duplicate_reason = engine._same_price_buy_guard(trade, source_settings)
                if duplicate_reason:
                    if evaluate_trade:
                        skipped_rows.append(
                            {
                                "source_trade_id": trade["source_trade_id"],
                                "market_slug": trade["market_slug"],
                                "outcome": trade["outcome"],
                                "side": trade["side"],
                                "reason": duplicate_reason,
                            }
                        )
                    continue
                requested_amount = round(float(trade.get("amount_usd") or 0.0), 2)
                if requested_amount <= 0:
                    if evaluate_trade:
                        skipped_rows.append(
                            {
                                "source_trade_id": trade["source_trade_id"],
                                "market_slug": trade["market_slug"],
                                "outcome": trade["outcome"],
                                "side": trade["side"],
                                "reason": "Source buy amount was zero.",
                            }
                        )
                    continue
                total_injected_cash += _ensure_cash(temp_db, requested_amount)
                order = _make_fill(trade, requested_amount, source_settings)
                broker._apply_fill(order, db_path=temp_db)
                database.insert_copy_order(order, temp_db)
                if evaluate_trade:
                    simulated_rows.append(order)
                continue

            if not int(source_settings["copy_sells"]):
                if evaluate_trade:
                    skipped_rows.append(
                        {
                            "source_trade_id": trade["source_trade_id"],
                            "market_slug": trade["market_slug"],
                            "outcome": trade["outcome"],
                            "side": trade["side"],
                            "reason": "Sell copying disabled.",
                        }
                    )
                continue

            local_position, match_strategy = engine._find_matching_local_position(trade)
            if not local_position or float(local_position.get("shares") or 0.0) <= 0:
                if evaluate_trade:
                    skipped_rows.append(
                        {
                            "source_trade_id": trade["source_trade_id"],
                            "market_slug": trade["market_slug"],
                            "outcome": trade["outcome"],
                            "side": trade["side"],
                            "reason": "No matching local inventory to sell.",
                        }
                    )
                continue

            requested_amount = round(float(trade.get("amount_usd") or 0.0), 2)
            if requested_amount <= 0:
                requested_amount = round(float(local_position.get("shares") or 0.0) * max(float(trade.get("price") or 0.0), 0.01), 2)
            order = _make_fill(
                trade,
                requested_amount,
                source_settings,
                position_key=local_position.get("position_key") or trade["position_key"],
                match_strategy=match_strategy,
            )
            order["shares"] = round(min(float(local_position.get("shares") or 0.0), float(order.get("shares") or 0.0)), 6)
            order["requested_amount_usd"] = round(order["shares"] * float(order["executed_price"]), 2)
            broker._apply_fill(order, db_path=temp_db)
            database.insert_copy_order(order, temp_db)
            if evaluate_trade:
                simulated_rows.append(order)

        simulated_positions = database.trade_analytics(temp_db)["open_positions"]

    actual_by_source: dict[str, list[dict]] = {}
    for row in actual_rows:
        actual_by_source.setdefault(row.get("source_trade_id") or "", []).append(row)

    simulated_by_source = {row.get("source_trade_id") or "": row for row in simulated_rows}
    skipped_by_source = {row.get("source_trade_id") or "": row for row in skipped_rows}
    evaluation_ids = sorted(set(simulated_by_source) | set(skipped_by_source) | set(actual_by_source))

    comparisons: list[dict] = []
    matched = 0
    mismatched = 0
    for source_trade_id in evaluation_ids:
        simulated = simulated_by_source.get(source_trade_id)
        skipped = skipped_by_source.get(source_trade_id)
        actual = actual_by_source.get(source_trade_id, [])
        if simulated and actual:
            matched += 1
            comparisons.append(
                {
                    "source_trade_id": source_trade_id,
                    "status": "matched",
                    "simulated_side": simulated.get("side"),
                    "simulated_amount_usd": simulated.get("requested_amount_usd"),
                    "actual_tables": sorted({row.get("compare_table") for row in actual}),
                }
            )
            continue
        mismatched += 1
        comparisons.append(
            {
                "source_trade_id": source_trade_id,
                "status": "mismatch",
                "simulated": simulated or skipped or None,
                "actual": actual,
            }
        )

    return {
        "source_db": str(source_db),
        "compare_db": str(compare_db) if compare_db else "",
        "compare_tables": compare_tables,
        "restrict_to_compare_source_trades": restrict_to_compare_source_trades,
        "ignored_already_owned_failures": ignore_already_owned_failures,
        "since_hours": since_hours,
        "summary": {
            "source_trades_total": len(source_trades),
            "compare_source_trades_total": len(compare_source_ids),
            "evaluated_source_trade_ids": len(evaluation_ids),
            "simulated_copies": len(simulated_rows),
            "simulated_skips": len(skipped_rows),
            "actual_rows_after_filter": len(actual_rows),
            "matched_simulated_vs_actual": matched,
            "mismatched_simulated_vs_actual": mismatched,
            "minimum_parity_cash_injected_usd": round(total_injected_cash, 2),
            "simulated_open_positions": len(simulated_positions),
        },
        "skip_reasons": dict(Counter(row["reason"] for row in skipped_rows)),
        "final_simulated_positions_sample": simulated_positions[:15],
        "comparisons_sample": comparisons[:25],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay source trades with leader-sized parity assumptions and compare them to actual live/shadow tables."
    )
    parser.add_argument("--source-db", required=True, help="SQLite DB that provides the source_trades stream to replay.")
    parser.add_argument("--compare-db", help="Optional SQLite DB containing actual live/shadow order tables to compare against.")
    parser.add_argument(
        "--compare-tables",
        default="copy_orders,live_order_attempts",
        help="Comma-separated tables to compare against. Supported: copy_orders, live_order_attempts, shadow_orders.",
    )
    parser.add_argument(
        "--restrict-to-compare-source-trades",
        action="store_true",
        help="Only score source_trade_id values that exist in the compare DB, while still replaying the full source history for inventory context.",
    )
    parser.add_argument(
        "--include-already-owned-failures",
        action="store_true",
        help="Do not filter compare-table failures that are only duplicate-holding or already-owned cases.",
    )
    parser.add_argument(
        "--since-hours",
        type=float,
        help="Only score source trades from the trailing N hours, while still replaying the full source history for inventory context.",
    )
    args = parser.parse_args()

    source_db = Path(args.source_db).expanduser().resolve()
    compare_db = Path(args.compare_db).expanduser().resolve() if args.compare_db else None
    compare_tables = [part.strip() for part in args.compare_tables.split(",") if part.strip()]

    report = _simulate_parity(
        source_db,
        compare_db=compare_db,
        compare_tables=compare_tables,
        restrict_to_compare_source_trades=args.restrict_to_compare_source_trades,
        ignore_already_owned_failures=not args.include_already_owned_failures,
        since_hours=args.since_hours,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
