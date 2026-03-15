from __future__ import annotations

import json
import re
import threading
import time
import uuid
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from . import database
from .config import DB_PATH
from .polymarket import PolymarketClient

MIN_BET_USD = 0.05
MEANINGFUL_MIN_BET_USD = 1.00
MIN_CASH_RESERVE_PCT = 0.20
MAX_SINGLE_BET_CASH_PCT = 0.20
MAX_TRADE_FETCH_LIMIT = 10
SOURCE_POSITION_FETCH_LIMIT = 200
BUY_REPLAY_GUARD_SECONDS = 15
FRESH_FILL_MARK_GRACE_SECONDS = 5
EXPOSURE_CAP_BUFFER_USD = 30.0
AUTO_PROFIT_TAKE_THRESHOLD = 0.70
FULLY_PRICED_EXIT_THRESHOLD = 0.999


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    if record.get("position_key"):
        aliases.add(f"pk:{record['position_key']}")
    market_slug = (record.get("market_slug") or payload.get("market_slug") or payload.get("marketSlug") or payload.get("slug") or "").strip().lower()
    if market_slug and outcome:
        aliases.add(f"slug:{market_slug}|{outcome}")
    market_title = _normalize_text(record.get("market_title") or payload.get("market_title") or payload.get("marketTitle") or payload.get("title") or "")
    if market_title and outcome:
        aliases.add(f"title:{market_title}|{outcome}")
    condition_id = (record.get("condition_id") or payload.get("condition_id") or payload.get("conditionId") or payload.get("marketId") or "").strip().lower()
    if condition_id:
        aliases.add(f"condition:{condition_id}")
    token_id = (record.get("token_id") or payload.get("token_id") or payload.get("tokenId") or payload.get("outcomeTokenId") or payload.get("asset") or "").strip().lower()
    if token_id:
        aliases.add(f"token:{token_id}")
    return aliases


def _round_up_to_cent(value: float) -> float:
    if value <= 0:
        return 0.0
    return math.ceil(value * 100) / 100.0


def _copy_trade_size(local_equity: float, source_amount_usd: float, leader_wallet_value: float, buying_capacity: float, cash_balance: float) -> float:
    local_equity = max(float(local_equity), 0.0)
    source_amount_usd = max(float(source_amount_usd), 0.0)
    leader_wallet_value = max(float(leader_wallet_value), 0.0)
    buying_capacity = max(float(buying_capacity), 0.0)
    cash_balance = max(float(cash_balance), 0.0)
    if local_equity <= 0:
        return 0.0

    if buying_capacity <= 0:
        return 0.0

    single_bet_cap = _round_up_to_cent(cash_balance * MAX_SINGLE_BET_CASH_PCT)
    effective_capacity = min(buying_capacity, single_bet_cap)
    if effective_capacity <= 0:
        return 0.0

    if source_amount_usd <= 0 or leader_wallet_value <= 0:
        return effective_capacity

    aggression_fraction = _clamp(source_amount_usd / leader_wallet_value, 0.0, 1.0)
    scaled_amount = _round_up_to_cent(local_equity * aggression_fraction)
    if scaled_amount >= MEANINGFUL_MIN_BET_USD:
        return min(scaled_amount, effective_capacity)

    # If proportional sizing would collapse into dust, deploy available cash instead.
    return effective_capacity


def _effective_exposure_cap(settings: dict, portfolio: dict) -> float:
    net_value = max(float(portfolio.get("net_value") or 0.0), 0.0)
    if net_value <= 100.0:
        return round(net_value, 2)
    return round(max(net_value - EXPOSURE_CAP_BUFFER_USD, 0.0), 2)


@dataclass
class CopyDecision:
    action: str
    reason: str
    requested_amount_usd: float = 0.0
    executed_price: float = 0.0
    shares: float = 0.0
    position_key: str = ""
    match_strategy: str = ""


class PaperBroker:
    def execute(self, source_trade: dict, requested_amount_usd: float, settings: dict, db_path=DB_PATH) -> dict:
        side = source_trade["side"]
        source_price = max(float(source_trade.get("price") or 0.0), 0.01)
        slippage = float(settings["slippage_bps"]) / 10000.0
        executed_price = source_price * (1 + slippage) if side == "BUY" else source_price * (1 - slippage)
        executed_price = _clamp(executed_price, 0.01, 0.99)
        shares = round(requested_amount_usd / executed_price, 6)
        order = {
            "order_id": str(uuid.uuid4()),
            "source_trade_id": source_trade["source_trade_id"],
            "market_slug": source_trade["market_slug"],
            "market_title": source_trade.get("market_title"),
            "outcome": source_trade["outcome"],
            "side": side,
            "requested_amount_usd": round(requested_amount_usd, 2),
            "executed_price": round(executed_price, 4),
            "shares": shares,
            "status": "FILLED",
            "failure_reason": None,
            "created_at": database.utc_now(),
            "position_key": source_trade.get("position_key", ""),
            "match_strategy": source_trade.get("match_strategy", ""),
        }
        self._apply_fill(order, db_path=db_path)
        return order

    def execute_manual(
        self,
        *,
        market_slug: str,
        market_title: str | None,
        outcome: str,
        side: str,
        price: float,
        requested_amount_usd: float,
        reason: str,
        settings: dict,
        db_path=DB_PATH,
    ) -> dict:
        requested_amount_usd = round(float(requested_amount_usd or 0.0), 2)
        source_price = max(float(price or 0.0), 0.0)
        slippage = float(settings["slippage_bps"]) / 10000.0
        if side == "SELL" and requested_amount_usd <= 0 and source_price <= 0:
            executed_price = 0.0
            position_key = f"{market_slug}:{outcome}"
            position = database.get_local_position(position_key, db_path)
            shares = round(float(position["shares"]) if position else 0.0, 6)
        else:
            source_price = max(source_price, 0.01)
            executed_price = source_price * (1 + slippage) if side == "BUY" else source_price * (1 - slippage)
            executed_price = _clamp(executed_price, 0.01, 0.99)
            shares = round(requested_amount_usd / executed_price, 6)
        order = {
            "order_id": str(uuid.uuid4()),
            "source_trade_id": None,
            "market_slug": market_slug,
            "market_title": market_title,
            "outcome": outcome,
            "side": side,
            "requested_amount_usd": requested_amount_usd,
            "executed_price": round(executed_price, 4),
            "shares": shares,
            "status": "FILLED",
            "failure_reason": reason,
            "created_at": database.utc_now(),
            "position_key": "",
            "match_strategy": "cash-reserve-rebalance",
        }
        self._apply_fill(order, db_path=db_path)
        return order

    def _apply_fill(self, order: dict, db_path=DB_PATH) -> None:
        settings = database.get_settings(db_path)
        cash = float(settings["paper_cash_balance"])
        position_key = order.get("position_key") or f"{order['market_slug']}:{order['outcome']}"
        position = database.get_local_position(position_key, db_path)
        cost = round(order["requested_amount_usd"], 2)

        if order["side"] == "BUY":
            if cost > cash + 1e-9:
                raise RuntimeError("Insufficient paper cash balance")
            new_cash = round(cash - cost, 2)
            existing_shares = float(position["shares"]) if position else 0.0
            existing_cost_basis = round(existing_shares * float(position["avg_price"]), 2) if position else 0.0
            new_shares = existing_shares + order["shares"]
            new_cost_basis = existing_cost_basis + cost
            avg_price = round(new_cost_basis / new_shares, 4) if new_shares else order["executed_price"]
            database.update_local_position(
                position_key,
                {
                    "market_slug": order["market_slug"],
                    "market_title": order.get("market_title"),
                    "outcome": order["outcome"],
                    "side": order["side"],
                    "shares": round(new_shares, 6),
                    "avg_price": avg_price,
                    "notional_usd": round(new_shares * order["executed_price"], 2),
                    "realized_pnl": float(position["realized_pnl"]) if position else 0.0,
                    "updated_at": database.utc_now(),
                },
                db_path,
            )
            database.update_settings({"paper_cash_balance": new_cash}, db_path)
            return

        if not position or float(position["shares"]) <= 0:
            raise RuntimeError("No local position available to sell")
        sell_shares = min(float(position["shares"]), order["shares"])
        avg_price = float(position["avg_price"])
        realized_pnl = float(position["realized_pnl"]) + sell_shares * (order["executed_price"] - avg_price)
        proceeds = round(sell_shares * order["executed_price"], 2)
        remaining_shares = round(float(position["shares"]) - sell_shares, 6)
        remaining_notional = round(max(remaining_shares * avg_price, 0.0), 2)
        database.update_local_position(
            position_key,
            {
                "market_slug": order["market_slug"],
                "market_title": order.get("market_title"),
                "outcome": order["outcome"],
                "side": order["side"],
                "shares": remaining_shares,
                "avg_price": round(avg_price, 4),
                "notional_usd": remaining_notional,
                "realized_pnl": round(realized_pnl, 2),
                "updated_at": database.utc_now(),
            },
            db_path,
        )
        database.update_settings({"paper_cash_balance": round(cash + proceeds, 2)}, db_path)


class ShadowBroker:
    def __init__(self, client: PolymarketClient):
        self.client = client

    def _resolve_market_context(self, source_trade: dict) -> tuple[str, dict]:
        token_id = (source_trade.get("token_id") or "").strip()
        market_context = {}
        if source_trade.get("market_slug"):
            market_context = self.client.fetch_market_metadata(source_trade.get("market_slug") or "")
        if not token_id:
            market_prices = self.client.fetch_market_prices([source_trade])
            match = next(
                (
                    row for row in market_prices
                    if (row.get("market_slug") or "") == (source_trade.get("market_slug") or "")
                    and (row.get("outcome") or "") == (source_trade.get("outcome") or "")
                ),
                None,
            )
            if match:
                token_id = (match.get("token_id") or "").strip()
                if not market_context:
                    market_context = {
                        "market_slug": match.get("market_slug") or "",
                        "category": match.get("category") or "",
                        "market_type": match.get("market_type") or "",
                        "sports_market_type": match.get("sports_market_type") or "",
                        "fees_enabled": bool(match.get("fees_enabled", False)),
                    }
        return token_id, market_context

    def _fee_exponent(self, market_context: dict) -> int:
        category = (market_context.get("category") or "").strip().lower()
        market_type = (market_context.get("market_type") or "").strip().lower()
        sports_market_type = (market_context.get("sports_market_type") or "").strip().lower()
        if "crypto" in category or "crypto" in market_type:
            return 2
        if sports_market_type:
            return 1
        return 0

    def _estimate_fee_usd(self, gross_notional_usd: float, live_price: float, fee_rate_bps: float, exponent: int) -> float:
        if gross_notional_usd <= 0 or live_price <= 0 or fee_rate_bps <= 0 or exponent <= 0:
            return 0.0
        fee_rate = fee_rate_bps / 10000.0
        price_term = live_price * (1 - live_price)
        return round(gross_notional_usd * fee_rate * (price_term**exponent), 6)

    def preview(self, source_trade: dict, requested_amount_usd: float, settings: dict) -> dict:
        side = source_trade["side"]
        source_price = max(float(source_trade.get("price") or 0.0), 0.01)
        paper_slippage = float(settings["slippage_bps"]) / 10000.0
        extra_live_slippage = float(settings.get("shadow_extra_slippage_bps") or 0.0) / 10000.0
        paper_price = source_price * (1 + paper_slippage) if side == "BUY" else source_price * (1 - paper_slippage)
        live_price = source_price * (1 + paper_slippage + extra_live_slippage) if side == "BUY" else source_price * (1 - paper_slippage - extra_live_slippage)
        paper_price = round(_clamp(paper_price, 0.01, 0.99), 4)
        live_price = round(_clamp(live_price, 0.01, 0.99), 4)
        estimated_live_shares = round(requested_amount_usd / live_price, 6) if live_price > 0 else 0.0
        token_id, market_context = self._resolve_market_context(source_trade)
        fees_enabled = bool(market_context.get("fees_enabled", False))
        fee_rate_bps = self.client.fetch_fee_rate_bps(token_id) if fees_enabled and token_id else 0.0
        fee_exponent = self._fee_exponent(market_context)
        fee_usd = self._estimate_fee_usd(requested_amount_usd, live_price, fee_rate_bps, fee_exponent) if fees_enabled else 0.0
        effective_live_price = live_price
        net_live_shares = estimated_live_shares
        if fee_usd > 0 and estimated_live_shares > 0:
            if side == "BUY":
                fee_shares = fee_usd / live_price if live_price > 0 else 0.0
                net_live_shares = max(estimated_live_shares - fee_shares, 0.0)
                if net_live_shares > 0:
                    effective_live_price = round(requested_amount_usd / net_live_shares, 4)
            else:
                net_proceeds = max(requested_amount_usd - fee_usd, 0.0)
                effective_live_price = round(net_proceeds / estimated_live_shares, 4) if estimated_live_shares > 0 else live_price
        effective_live_price = round(_clamp(effective_live_price, 0.01, 0.99), 4)
        price_delta_bps = ((live_price - paper_price) / paper_price) * 10000 if paper_price > 0 else 0.0
        if fee_usd > 0 and effective_live_price > 0:
            price_delta_bps = ((effective_live_price - paper_price) / paper_price) * 10000 if paper_price > 0 else 0.0
        if side == "SELL":
            price_delta_bps *= -1
        price_delta_cents = round((effective_live_price - paper_price) * 100, 3)
        execution_drag_usd = round(
            requested_amount_usd - (round(net_live_shares, 6) * paper_price if side == "BUY" else round(net_live_shares, 6) * effective_live_price),
            4,
        ) if side == "BUY" else round(
            (round(net_live_shares, 6) * paper_price) - (round(net_live_shares, 6) * effective_live_price),
            4,
        )
        return {
            "shadow_order_id": str(uuid.uuid4()),
            "source_trade_id": source_trade.get("source_trade_id"),
            "market_slug": source_trade.get("market_slug"),
            "market_title": source_trade.get("market_title"),
            "outcome": source_trade.get("outcome"),
            "side": side,
            "requested_amount_usd": round(requested_amount_usd, 2),
            "reference_price": round(source_price, 4),
            "paper_price": paper_price,
            "estimated_live_price": effective_live_price,
            "estimated_live_shares": round(net_live_shares, 6),
            "price_delta_bps": round(price_delta_bps, 2),
            "price_delta_cents": price_delta_cents,
            "execution_drag_usd": execution_drag_usd,
            "status": "SHADOW",
            "created_at": database.utc_now(),
            "position_key": source_trade.get("position_key", ""),
            "match_strategy": source_trade.get("match_strategy", ""),
            "extra_slippage_bps": float(settings.get("shadow_extra_slippage_bps") or 0.0),
            "token_id": token_id,
            "fees_enabled": fees_enabled,
            "fee_rate_bps": fee_rate_bps,
            "estimated_fee_usd": round(fee_usd, 6),
        }


class LiveBroker:
    def __init__(self, client: PolymarketClient):
        self.client = client

    def execute(self, source_trade: dict, requested_amount_usd: float, settings: dict, db_path=DB_PATH) -> dict:
        raise RuntimeError(
            "Live execution mode is scaffolded but real order submission is not implemented. "
            "Add signed CLOB order placement, fill reconciliation, and live balance tracking before enabling it."
        )

    def execute_manual(
        self,
        *,
        market_slug: str,
        market_title: str | None,
        outcome: str,
        side: str,
        price: float,
        requested_amount_usd: float,
        reason: str,
        settings: dict,
        db_path=DB_PATH,
    ) -> dict:
        raise RuntimeError(
            "Live execution mode is scaffolded but manual live order submission is not implemented. "
            "Add signed CLOB sell/buy support before using live mode."
        )


class CopyTradingEngine:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.client = PolymarketClient()
        self.broker = PaperBroker()
        self.shadow_broker = ShadowBroker(self.client)
        self.live_broker = LiveBroker(self.client)
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._latest_results = []
        self._last_tick_started_at = ""
        self._last_tick_finished_at = ""
        self._last_tick_status = "idle"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="copy-engine")
        self._thread.start()
        database.log("INFO", "engine", "Background engine started.", db_path=self.db_path)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            settings = database.get_settings(self.db_path)
            app_state = database.get_app_state(self.db_path)
            interval_ms = max(int(settings["sync_interval_ms"]), 500)
            if app_state["engine_status"] == "RUNNING":
                try:
                    self.tick()
                except Exception as exc:
                    database.set_app_state("last_error", str(exc), self.db_path)
                    database.log("ERROR", "engine", "Background sync failed.", {"error": str(exc)}, self.db_path)
            self._stop_event.wait(interval_ms / 1000.0)

    def set_running(self, enabled: bool) -> None:
        database.set_app_state("engine_status", "RUNNING" if enabled else "PAUSED", self.db_path)
        database.log("INFO", "engine", f"Engine set to {'RUNNING' if enabled else 'PAUSED'}.", db_path=self.db_path)

    def tick(self, force: bool = False) -> dict:
        with self._lock:
            return self._tick(force=force)

    def _tick(self, force: bool = False) -> dict:
        settings = database.get_settings(self.db_path)
        app_state = database.get_app_state(self.db_path)
        if app_state["engine_status"] != "RUNNING" and not force:
            return {"status": "PAUSED", "message": "Engine paused."}

        self._last_tick_started_at = database.utc_now()
        self._last_tick_status = "running"
        run_id = database.insert_sync_run(db_path=self.db_path)
        started = time.perf_counter()
        trades_seen = 0
        new_trades = 0
        copied = 0
        failed = 0
        message = "Sync complete."
        try:
            target = settings["target_wallet"] or settings["target_handle"]
            profile = self.client.resolve_target_wallet(target)
            previous_local_positions = database.get_local_positions(self.db_path)
            trade_fetch_limit = max(1, min(int(settings["trade_fetch_limit"]), MAX_TRADE_FETCH_LIMIT))
            trades = self.client.fetch_trades(profile["wallet"], handle=profile["handle"], limit=trade_fetch_limit)
            positions = self.client.fetch_positions(profile["wallet"], limit=SOURCE_POSITION_FETCH_LIMIT)
            leader_wallet_value = self._get_leader_wallet_value(settings, profile["wallet"], positions)
            bootstrap_copied = self._bootstrap_current_source_positions(
                settings,
                app_state,
                profile,
                positions,
                leader_wallet_value,
            )
            previous_positions = {
                row["position_key"]: row
                for row in database.list_source_positions(1000, self.db_path)
            }
            synthetic_sell_trades = self._build_position_reduction_sells(
                previous_positions,
                positions,
                trades,
                profile["wallet"],
                profile["handle"],
            )
            database.replace_source_positions(positions, self.db_path)
            database.refresh_local_position_market_values(self.db_path, source_positions=positions)
            trades_seen = len(trades)
            all_seen_trades = trades + synthetic_sell_trades
            trade_ids = [trade["source_trade_id"] for trade in all_seen_trades]
            existing_trade_ids = database.get_existing_source_trade_ids(trade_ids, self.db_path)
            fresh_trades = [trade for trade in all_seen_trades if trade["source_trade_id"] not in existing_trade_ids]
            eligible_fresh_trades, prestart_trades = self._split_by_copy_start(fresh_trades, app_state.get("copy_start_at", ""))
            new_trades = len(fresh_trades)

            copied, failed = self._copy_trades_first(
                sorted(eligible_fresh_trades, key=lambda item: item["created_at"], reverse=True),
                settings,
                leader_wallet_value,
                positions,
            )
            database.upsert_source_trades(all_seen_trades, self.db_path)
            self._finalize_fresh_trade_records(eligible_fresh_trades)
            self._mark_prestart_trades(prestart_trades, app_state.get("copy_start_at", ""))
            backlog_copied, backlog_failed = self._copy_pending(settings, leader_wallet_value, app_state.get("copy_start_at", ""), positions)
            copied += backlog_copied
            failed += backlog_failed
            copied += bootstrap_copied
            database.refresh_local_position_market_values(self.db_path, freeze_recent_seconds=FRESH_FILL_MARK_GRACE_SECONDS, source_positions=positions)
            auto_zero_value_exits = self._liquidate_zero_value_positions(settings, positions)
            copied += auto_zero_value_exits
            if auto_zero_value_exits:
                database.refresh_local_position_market_values(self.db_path, source_positions=positions)
            auto_profit_takes = self._liquidate_positions_at_profit_target(settings, positions)
            copied += auto_profit_takes
            if auto_profit_takes:
                database.refresh_local_position_market_values(self.db_path, source_positions=positions)
            auto_liquidated = self._liquidate_fully_priced_positions(settings, positions)
            copied += auto_liquidated
            if auto_liquidated:
                database.refresh_local_position_market_values(self.db_path, source_positions=positions)
            reconciliation_mismatches = self._reconcile_source_sells(
                previous_local_positions=previous_local_positions,
                current_local_positions=database.get_local_positions(self.db_path),
                source_sell_trades=all_seen_trades,
                copy_start_at=app_state.get("copy_start_at", ""),
            )
            portfolio = database.portfolio_totals(self.db_path, positions)
            database.snapshot_portfolio(
                portfolio["cash_balance"],
                portfolio["gross_exposure"],
                portfolio["net_value"],
                portfolio["positions_count"],
                self.db_path,
            )
            shadow_portfolio = database.shadow_portfolio_totals(self.db_path, positions)
            database.snapshot_shadow_portfolio(
                shadow_portfolio["cash_balance"],
                shadow_portfolio["gross_exposure"],
                shadow_portfolio["net_value"],
                shadow_portfolio["positions_count"],
                self.db_path,
            )
            database.set_app_state("resolved_target_wallet", profile["wallet"], self.db_path)
            database.set_app_state("leader_wallet_value", f"{leader_wallet_value:.8f}", self.db_path)
            database.set_app_state("leader_wallet_updated_at", database.utc_now(), self.db_path)
            database.set_app_state("last_sync_at", database.utc_now(), self.db_path)
            status_note = f"Synced {trades_seen} source trades, copied {copied}."
            if reconciliation_mismatches:
                status_note = f"{status_note} Sell mismatches: {reconciliation_mismatches}."
                database.set_app_state("last_error", f"{reconciliation_mismatches} sell reconciliation mismatch(es) detected.", self.db_path)
            else:
                database.set_app_state("last_error", "", self.db_path)
            database.set_app_state("last_sync_message", status_note, self.db_path)
            message = f"Synced {trades_seen} trades, {new_trades} new, {copied} copied."
            if reconciliation_mismatches:
                message = f"{message} Sell mismatches: {reconciliation_mismatches}."
            database.log("INFO", "sync", message, {"target_wallet": profile["wallet"]}, self.db_path)
            status = "SUCCESS"
        except Exception as exc:
            status = "ERROR"
            message = str(exc)
            database.set_app_state("last_error", message, self.db_path)
            database.log("ERROR", "sync", "Sync run failed.", {"error": message}, self.db_path)

        latency_ms = int((time.perf_counter() - started) * 1000)
        database.finish_sync_run(
            run_id,
            status=status,
            trades_seen=trades_seen,
            new_trades=new_trades,
            copied=copied,
            failed=failed,
            latency_ms=latency_ms,
            message=message,
            db_path=self.db_path,
        )
        self._last_tick_finished_at = database.utc_now()
        self._last_tick_status = status.lower()
        return {"status": status, "message": message, "latency_ms": latency_ms}

    def health(self) -> dict:
        app_state = database.get_app_state(self.db_path)
        live_positions = database.fetch_live_source_positions(self.db_path)
        portfolio = database.portfolio_totals(self.db_path, live_positions)
        return {
            "status": "ok" if app_state.get("engine_status") in {"RUNNING", "PAUSED"} else "degraded",
            "engine_status": app_state.get("engine_status"),
            "last_sync_at": app_state.get("last_sync_at"),
            "last_error": app_state.get("last_error"),
            "last_tick_started_at": self._last_tick_started_at,
            "last_tick_finished_at": self._last_tick_finished_at,
            "last_tick_status": self._last_tick_status,
            "net_value": portfolio["net_value"],
            "cash_balance": portfolio["cash_balance"],
        }

    def _execution_mode(self, settings: dict) -> str:
        mode = (settings.get("execution_mode") or "paper").strip().lower()
        if mode == "shadow":
            return "shadow"
        if mode == "live":
            return "live"
        return "paper"

    def _active_broker(self, settings: dict):
        return self.live_broker if self._execution_mode(settings) == "live" else self.broker

    def _record_shadow_preview(self, trade: dict, requested_amount_usd: float, settings: dict) -> dict | None:
        if self._execution_mode(settings) != "shadow":
            return None
        preview = self.shadow_broker.preview(trade, requested_amount_usd, settings)
        database.insert_shadow_order(preview, self.db_path)
        return preview

    def _execute_copy_trade(self, trade: dict, decision: CopyDecision, settings: dict) -> tuple[dict, dict | None]:
        executable_trade = dict(trade)
        if decision.position_key:
            executable_trade["position_key"] = decision.position_key
        if decision.match_strategy:
            executable_trade["match_strategy"] = decision.match_strategy
        order = self._active_broker(settings).execute(executable_trade, decision.requested_amount_usd, settings, self.db_path)
        database.insert_copy_order(order, self.db_path)
        shadow_preview = self._record_shadow_preview(executable_trade, decision.requested_amount_usd, settings)
        return order, shadow_preview

    def _execute_manual_trade(
        self,
        *,
        market_slug: str,
        market_title: str | None,
        outcome: str,
        side: str,
        price: float,
        requested_amount_usd: float,
        reason: str,
        settings: dict,
        match_strategy: str,
    ) -> tuple[dict, dict | None]:
        order = self._active_broker(settings).execute_manual(
            market_slug=market_slug,
            market_title=market_title,
            outcome=outcome,
            side=side,
            price=price,
            requested_amount_usd=requested_amount_usd,
            reason=reason,
            settings=settings,
            db_path=self.db_path,
        )
        order["match_strategy"] = match_strategy
        database.insert_copy_order(order, self.db_path)
        shadow_preview = self._record_shadow_preview(
            {
                "source_trade_id": None,
                "market_slug": market_slug,
                "market_title": market_title,
                "outcome": outcome,
                "side": side,
                "price": price,
                "position_key": "",
                "match_strategy": match_strategy,
            },
            requested_amount_usd,
            settings,
        )
        return order, shadow_preview

    def _copy_trades_first(self, trades: list[dict], settings: dict, leader_wallet_value: float, source_positions: list[dict]) -> tuple[int, int]:
        copied = 0
        failed = 0
        self._latest_results = []
        for trade in trades:
            decision = self._decide_copy_trade(trade, settings, leader_wallet_value, source_positions)
            result = {
                "trade_id": trade["source_trade_id"],
                "copy_status": "skipped",
                "copied_order_id": None,
                "last_error": decision.reason,
                "log_level": "INFO",
                "log_message": f"Skipped {trade['source_trade_id']}.",
                "log_details": {"reason": decision.reason},
            }
            if decision.action == "copy":
                try:
                    order, shadow_preview = self._execute_copy_trade(trade, decision, settings)
                    log_details = {"amount_usd": order["requested_amount_usd"], "price": order["executed_price"]}
                    if shadow_preview:
                        log_details["shadow_live_price"] = shadow_preview["estimated_live_price"]
                        log_details["shadow_price_delta_bps"] = shadow_preview["price_delta_bps"]
                    result = {
                        "trade_id": trade["source_trade_id"],
                        "copy_status": "copied",
                        "copied_order_id": order["order_id"],
                        "last_error": None,
                        "log_level": "INFO",
                        "log_message": f"Copied {trade['side']} {trade['market_slug']} {trade['outcome']}.",
                        "log_details": log_details,
                    }
                    copied += 1
                except Exception as exc:
                    result = {
                        "trade_id": trade["source_trade_id"],
                        "copy_status": "failed",
                        "copied_order_id": None,
                        "last_error": str(exc),
                        "log_level": "ERROR",
                        "log_message": f"Copy failed for {trade['source_trade_id']}.",
                        "log_details": {"error": str(exc)},
                    }
                    failed += 1
            self._latest_results.append(result)
        return copied, failed

    def _finalize_fresh_trade_records(self, trades: list[dict]) -> None:
        if not trades:
            return
        for result in getattr(self, "_latest_results", []):
            database.mark_source_trade(
                result["trade_id"],
                result["copy_status"],
                copied_order_id=result["copied_order_id"],
                last_error=result["last_error"],
                db_path=self.db_path,
            )
            database.log(
                result["log_level"],
                "copy",
                result["log_message"],
                result["log_details"],
                self.db_path,
            )

    def _copy_pending(self, settings: dict, leader_wallet_value: float, copy_start_at: str, source_positions: list[dict]) -> tuple[int, int]:
        copied = 0
        failed = 0
        for trade in database.list_pending_source_trades(self.db_path):
            if self._is_before_start(trade["created_at"], copy_start_at):
                database.mark_source_trade(
                    trade["source_trade_id"],
                    "ignored",
                    last_error=f"Trade happened before copy start {copy_start_at}.",
                    db_path=self.db_path,
                )
                continue
            decision = self._decide_copy_trade(trade, settings, leader_wallet_value, source_positions)
            if decision.action == "skip":
                database.mark_source_trade(trade["source_trade_id"], "skipped", last_error=decision.reason, db_path=self.db_path)
                database.log("INFO", "copy", f"Skipped {trade['source_trade_id']}.", {"reason": decision.reason}, self.db_path)
                continue
            try:
                order, shadow_preview = self._execute_copy_trade(trade, decision, settings)
                database.mark_source_trade(trade["source_trade_id"], "copied", copied_order_id=order["order_id"], db_path=self.db_path)
                log_details = {"amount_usd": order["requested_amount_usd"], "price": order["executed_price"]}
                if shadow_preview:
                    log_details["shadow_live_price"] = shadow_preview["estimated_live_price"]
                    log_details["shadow_price_delta_bps"] = shadow_preview["price_delta_bps"]
                database.log(
                    "INFO",
                    "copy",
                    f"Copied {trade['side']} {trade['market_slug']} {trade['outcome']}.",
                    log_details,
                    self.db_path,
                )
                copied += 1
            except Exception as exc:
                database.mark_source_trade(trade["source_trade_id"], "failed", last_error=str(exc), db_path=self.db_path)
                failed += 1
                database.log("ERROR", "copy", f"Copy failed for {trade['source_trade_id']}.", {"error": str(exc)}, self.db_path)
        return copied, failed

    def _decide_copy_trade(self, trade: dict, settings: dict, leader_wallet_value: float, source_positions: list[dict]) -> CopyDecision:
        side = trade["side"]
        portfolio = database.portfolio_totals(self.db_path, source_positions)
        local_equity = max(float(portfolio["net_value"]), 0.0)
        cash_balance = float(portfolio["cash_balance"])
        effective_exposure_cap = _effective_exposure_cap(settings, portfolio)
        remaining_exposure = round(effective_exposure_cap - portfolio["gross_exposure"], 2)
        buying_capacity = min(float(portfolio["cash_balance"]), remaining_exposure)
        requested_amount = _copy_trade_size(local_equity, trade.get("amount_usd") or 0.0, leader_wallet_value, buying_capacity, cash_balance)

        if side == "SELL" and not int(settings["copy_sells"]):
            return CopyDecision("skip", "Sell copying disabled.")

        if side == "BUY":
            if buying_capacity < MIN_BET_USD:
                return CopyDecision("skip", "No remaining buying capacity.")
            duplicate_reason = self._same_price_buy_guard(trade, settings)
            if duplicate_reason:
                return CopyDecision("skip", duplicate_reason)
            requested_amount = max(requested_amount, MIN_BET_USD)
            requested_amount = min(requested_amount, buying_capacity)
            requested_amount = _round_up_to_cent(requested_amount) if requested_amount > 0 else 0.0
            return CopyDecision("copy", "Buy trade eligible.", requested_amount_usd=requested_amount)

        local_position, match_strategy = self._find_matching_local_position(trade)
        if not local_position or float(local_position.get("shares") or 0.0) <= 0:
            return CopyDecision("skip", "No matching local inventory to sell.")
        price = max(float(trade.get("price") or 0.0), 0.01)
        max_sell_notional = round(float(local_position["shares"]) * price, 2)
        if max_sell_notional < MIN_BET_USD:
            return CopyDecision("skip", "Remaining position is too small to sell.")
        requested_amount = max(requested_amount, MIN_BET_USD)
        requested_amount = min(requested_amount, max_sell_notional)
        requested_amount = _round_up_to_cent(requested_amount) if requested_amount > 0 else 0.0
        return CopyDecision(
            "copy",
            "Sell trade eligible.",
            requested_amount_usd=requested_amount,
            position_key=local_position["position_key"],
            match_strategy=match_strategy,
        )

    def _liquidate_positions_at_profit_target(self, settings: dict, source_positions: list[dict]) -> int:
        positions = [
            row
            for row in database.list_local_positions_marked(
                self.db_path,
                freeze_recent_seconds=FRESH_FILL_MARK_GRACE_SECONDS,
                source_positions=source_positions,
            )
            if float(row.get("shares") or 0.0) > 0
            and float(row.get("market_value") or 0.0) >= MIN_BET_USD
            and float(row.get("cost_basis") or 0.0) > 0
            and float(row.get("unrealized_pnl") or 0.0) / float(row.get("cost_basis") or 1.0) >= AUTO_PROFIT_TAKE_THRESHOLD
        ]
        if not positions:
            return 0

        liquidated = 0
        for position in positions:
            market_value = round(float(position.get("market_value") or 0.0), 2)
            if market_value < MIN_BET_USD:
                continue
            self._execute_manual_trade(
                market_slug=position["market_slug"],
                market_title=position.get("market_title"),
                outcome=position["outcome"],
                side="SELL",
                price=float(position["current_price"]),
                requested_amount_usd=market_value,
                reason=f"Auto-sold after reaching {int(AUTO_PROFIT_TAKE_THRESHOLD * 100)}% profit.",
                settings=settings,
                match_strategy="auto-profit-take",
            )
            liquidated += 1

        if liquidated:
            database.log(
                "INFO",
                "rebalance",
                "Auto-sold paper positions after hitting the profit target.",
                {"positions_liquidated": liquidated, "profit_target_pct": AUTO_PROFIT_TAKE_THRESHOLD},
                self.db_path,
            )
        return liquidated

    def _liquidate_fully_priced_positions(self, settings: dict, source_positions: list[dict]) -> int:
        positions = [
            row
            for row in database.list_local_positions_marked(self.db_path, source_positions=source_positions)
            if float(row.get("shares") or 0.0) > 0
            and float(row.get("current_price") or 0.0) >= FULLY_PRICED_EXIT_THRESHOLD
            and float(row.get("market_value") or 0.0) >= MIN_BET_USD
        ]
        if not positions:
            return 0

        liquidated = 0
        for position in positions:
            market_value = round(float(position.get("market_value") or 0.0), 2)
            if market_value < MIN_BET_USD:
                continue
            self._execute_manual_trade(
                market_slug=position["market_slug"],
                market_title=position.get("market_title"),
                outcome=position["outcome"],
                side="SELL",
                price=float(position["current_price"]),
                requested_amount_usd=market_value,
                reason="Auto-sold fully priced position at 100c.",
                settings=settings,
                match_strategy="auto-full-price-exit",
            )
            liquidated += 1

        if liquidated:
            database.log(
                "INFO",
                "rebalance",
                "Auto-sold fully priced paper positions.",
                {"positions_liquidated": liquidated, "threshold_price": FULLY_PRICED_EXIT_THRESHOLD},
                self.db_path,
            )
        return liquidated

    def _liquidate_zero_value_positions(self, settings: dict, source_positions: list[dict]) -> int:
        positions = [
            row
            for row in database.list_local_positions_marked(self.db_path, source_positions=source_positions)
            if float(row.get("shares") or 0.0) > 0
            and float(row.get("current_price") or 0.0) <= 0.0001
            and float(row.get("market_value") or 0.0) <= 0.01
        ]
        if not positions:
            return 0

        liquidated = 0
        for position in positions:
            self._execute_manual_trade(
                market_slug=position["market_slug"],
                market_title=position.get("market_title"),
                outcome=position["outcome"],
                side="SELL",
                price=0.0,
                requested_amount_usd=0.0,
                reason="Auto-closed resolved losing position at 0c.",
                settings=settings,
                match_strategy="auto-zero-value-exit",
            )
            liquidated += 1

        if liquidated:
            database.log(
                "INFO",
                "rebalance",
                "Auto-closed zero-value paper positions.",
                {"positions_liquidated": liquidated},
                self.db_path,
            )
        return liquidated

    def _get_leader_wallet_value(self, settings: dict, profile_wallet: str, target_positions: list[dict]) -> float:
        reference_wallet = (settings.get("leader_wallet_address") or profile_wallet or "").strip()
        app_state = database.get_app_state(self.db_path)
        cached_value = float(app_state.get("leader_wallet_value") or 0.0)
        updated_at = app_state.get("leader_wallet_updated_at") or ""
        if updated_at:
            try:
                age_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(updated_at.replace("Z", "+00:00"))).total_seconds()
                if age_seconds < 15 and cached_value > 0:
                    return cached_value
            except ValueError:
                pass

        if reference_wallet.lower() == (profile_wallet or "").lower():
            return round(sum(float(item.get("notional_usd") or 0.0) for item in target_positions), 8)

        positions = self.client.fetch_positions(reference_wallet, limit=200)
        return round(sum(float(item.get("notional_usd") or 0.0) for item in positions), 8)

    def _bootstrap_current_source_positions(
        self,
        settings: dict,
        app_state: dict,
        profile: dict,
        positions: list[dict],
        leader_wallet_value: float,
    ) -> int:
        if app_state.get("bootstrap_positions_done_at"):
            return 0
        if database.get_local_positions(self.db_path):
            database.set_app_state("bootstrap_positions_done_at", database.utc_now(), self.db_path)
            return 0
        if database.list_copy_orders(1, self.db_path):
            database.set_app_state("bootstrap_positions_done_at", database.utc_now(), self.db_path)
            return 0
        if not positions or leader_wallet_value <= 0:
            return 0

        portfolio = database.portfolio_totals(self.db_path, positions)
        remaining_cash = float(portfolio["cash_balance"])
        effective_exposure_cap = _effective_exposure_cap(settings, portfolio)
        remaining_exposure = round(effective_exposure_cap - float(portfolio["gross_exposure"]), 2)
        buying_capacity = min(remaining_cash, remaining_exposure)
        if buying_capacity < MIN_BET_USD:
            database.set_app_state("bootstrap_positions_done_at", database.utc_now(), self.db_path)
            return 0

        copied = 0
        bootstrap_time = app_state.get("copy_start_at") or database.utc_now()
        ordered_positions = sorted(positions, key=lambda row: float(row.get("notional_usd") or 0.0), reverse=True)
        for position in ordered_positions:
            if buying_capacity < MIN_BET_USD:
                break
            weight = float(position.get("notional_usd") or 0.0) / leader_wallet_value if leader_wallet_value > 0 else 0.0
            if weight <= 0:
                continue
            requested_amount = _round_up_to_cent(float(portfolio["net_value"]) * weight)
            requested_amount = min(requested_amount, buying_capacity)
            if requested_amount < MIN_BET_USD:
                continue
            bootstrap_trade = {
                "source_trade_id": f"bootstrap:{position['position_key']}:{bootstrap_time}",
                "source_handle": profile.get("handle"),
                "source_wallet": profile.get("wallet"),
                "market_slug": position.get("market_slug"),
                "market_title": position.get("market_title"),
                "outcome": position.get("outcome"),
                "side": "BUY",
                "price": float(position.get("price") or 0.0),
                "shares": round(requested_amount / max(float(position.get("price") or 0.0), 0.01), 6),
                "amount_usd": requested_amount,
                "created_at": bootstrap_time,
                "status": "CONFIRMED",
                "condition_id": position.get("condition_id", ""),
                "token_id": position.get("token_id", ""),
                "position_key": position.get("position_key", ""),
                "match_strategy": "bootstrap-current-holdings",
            }
            self._execute_copy_trade(
                bootstrap_trade,
                CopyDecision("copy", "Bootstrap position eligible.", requested_amount_usd=requested_amount),
                settings,
            )
            copied += 1
            buying_capacity = round(buying_capacity - requested_amount, 2)

        database.set_app_state("bootstrap_positions_done_at", database.utc_now(), self.db_path)
        if copied:
            database.log(
                "INFO",
                "bootstrap",
                "Bootstrapped current source positions into local paper inventory.",
                {"positions_copied": copied, "bootstrap_time": bootstrap_time},
                self.db_path,
            )
        return copied

    def _build_position_reduction_sells(
        self,
        previous_positions: dict[str, dict],
        current_positions: list[dict],
        explicit_trades: list[dict],
        wallet: str,
        handle: str,
    ) -> list[dict]:
        synthetic_trades = []
        now = database.utc_now()
        explicit_sell_shares: dict[str, float] = {}
        for trade in explicit_trades:
            if trade.get("side") != "SELL":
                continue
            for alias in _position_aliases(trade):
                explicit_sell_shares[alias] = round(explicit_sell_shares.get(alias, 0.0) + float(trade.get("shares") or 0.0), 6)
        current_aliases = self._build_alias_index(current_positions)
        previous_seen: set[str] = set()
        for previous in previous_positions.values():
            position_key = previous.get("position_key") or f"{previous.get('market_slug')}:{previous.get('outcome')}"
            current = self._resolve_alias_match(previous, current_aliases)
            previous_seen.add(previous["position_key"])
            previous_shares = float(previous.get("shares") or 0.0)
            current_shares = float(current.get("shares") or 0.0) if current else 0.0
            explicit_shares = max((explicit_sell_shares.get(alias, 0.0) for alias in _position_aliases(previous)), default=0.0)
            share_delta = round(previous_shares - current_shares - explicit_shares, 6)
            if share_delta <= 1e-6:
                continue
            price = float((current or previous).get("price") or 0.0)
            if price <= 0:
                price = float(previous.get("notional_usd") or 0.0) / previous_shares if previous_shares > 0 else 0.0
            if price <= 0:
                continue
            synthetic_trades.append(
                {
                    "source_trade_id": f"position-sync:{position_key}:{current_shares:.6f}:{now}",
                    "source_handle": handle,
                    "source_wallet": wallet,
                    "market_slug": previous.get("market_slug"),
                    "market_title": previous.get("market_title"),
                    "outcome": previous.get("outcome"),
                    "side": "SELL",
                    "price": round(price, 4),
                    "shares": share_delta,
                    "amount_usd": round(share_delta * price, 2),
                    "created_at": now,
                    "status": "CONFIRMED",
                    "match_strategy": "source-position-delta",
                    "position_key": position_key,
                }
            )
        return synthetic_trades

    def _build_alias_index(self, records: list[dict]) -> dict[str, list[dict]]:
        alias_index: dict[str, list[dict]] = {}
        for record in records:
            for alias in _position_aliases(record):
                alias_index.setdefault(alias, []).append(record)
        return alias_index

    def _resolve_alias_match(self, record: dict, alias_index: dict[str, list[dict]]) -> dict | None:
        candidates: dict[str, tuple[dict, str]] = {}
        record_aliases = _position_aliases(record)
        for alias in record_aliases:
            for candidate in alias_index.get(alias, []):
                key = candidate.get("position_key") or f"{candidate.get('market_slug')}:{candidate.get('outcome')}"
                if key not in candidates:
                    candidates[key] = (candidate, alias)
        if not candidates:
            return None
        return max(
            candidates.values(),
            key=lambda row: (
                len(record_aliases.intersection(_position_aliases(row[0]))),
                float(row[0].get("shares") or 0.0),
                float(row[0].get("notional_usd") or row[0].get("market_value") or 0.0),
            ),
        )[0]

    def _find_matching_local_position(self, trade: dict) -> tuple[dict | None, str]:
        return self._find_matching_position_in_records(trade, database.get_local_positions(self.db_path))

    def _find_matching_marked_local_position(self, trade: dict, source_positions: list[dict]) -> tuple[dict | None, str]:
        return self._find_matching_position_in_records(
            trade,
            database.list_local_positions_marked(self.db_path, source_positions=source_positions),
        )

    def _same_price_buy_guard(self, trade: dict, settings: dict) -> str | None:
        position_key = f"{trade['market_slug']}:{trade['outcome']}"
        latest_order = database.get_latest_copy_order_for_position(position_key, self.db_path)
        if not latest_order or latest_order.get("side") != "BUY":
            return None
        latest_order_time = _parse_iso(latest_order.get("created_at") or "")
        source_trade_time = _parse_iso(trade.get("created_at") or "")
        if latest_order_time and source_trade_time:
            age_seconds = abs((latest_order_time - source_trade_time).total_seconds())
            if age_seconds <= BUY_REPLAY_GUARD_SECONDS:
                return "Already copied a recent buy for this outcome."
        source_price = max(float(trade.get("price") or 0.0), 0.01)
        slippage = float(settings["slippage_bps"]) / 10000.0
        executed_price = round(_clamp(source_price * (1 + slippage), 0.01, 0.99), 4)
        latest_price = round(float(latest_order.get("executed_price") or 0.0), 4)
        if abs(executed_price - latest_price) < 0.0001:
            return "Already bought this outcome at the current price."
        return None

    def _find_matching_position_in_records(self, trade: dict, records: list[dict]) -> tuple[dict | None, str]:
        alias_index = self._build_alias_index(records)
        direct_key = f"{trade['market_slug']}:{trade['outcome']}"
        direct_position = next((row for row in records if row.get("position_key") == direct_key), None)
        if direct_position and float(direct_position.get("shares") or 0.0) > 0:
            return direct_position, "direct-position-key"
        trade_aliases = _position_aliases(trade)
        matched_position = self._resolve_alias_match(trade, alias_index)
        if not matched_position:
            return None, ""
        shared_aliases = trade_aliases.intersection(_position_aliases(matched_position))
        if not shared_aliases:
            return matched_position, "alias-fallback"
        matched_alias = sorted(shared_aliases)[0]
        alias_type = matched_alias.split(":", 1)[0]
        return matched_position, f"alias-{alias_type}"

    def _reconcile_source_sells(
        self,
        *,
        previous_local_positions: list[dict],
        current_local_positions: list[dict],
        source_sell_trades: list[dict],
        copy_start_at: str,
    ) -> int:
        mismatch_count = 0
        current_by_key = {row["position_key"]: row for row in current_local_positions}
        seen_position_keys: set[str] = set()
        for trade in source_sell_trades:
            if trade.get("side") != "SELL" or self._is_before_start(trade.get("created_at", ""), copy_start_at):
                continue
            previous_position, match_strategy = self._find_matching_position_in_records(trade, previous_local_positions)
            if not previous_position:
                continue
            position_key = previous_position["position_key"]
            if position_key in seen_position_keys:
                continue
            seen_position_keys.add(position_key)
            previous_shares = float(previous_position.get("shares") or 0.0)
            current_position = current_by_key.get(position_key)
            current_shares = float(current_position.get("shares") or 0.0) if current_position else 0.0
            if current_shares < previous_shares - 1e-6:
                continue
            mismatch_count += 1
            database.log(
                "ERROR",
                "reconcile",
                "Source sell did not reduce matched local inventory.",
                {
                    "source_trade_id": trade.get("source_trade_id"),
                    "market_slug": trade.get("market_slug"),
                    "outcome": trade.get("outcome"),
                    "position_key": position_key,
                    "match_strategy": trade.get("match_strategy") or match_strategy,
                    "previous_local_shares": round(previous_shares, 6),
                    "current_local_shares": round(current_shares, 6),
                },
                self.db_path,
            )
        return mismatch_count

    def _split_by_copy_start(self, trades: list[dict], copy_start_at: str) -> tuple[list[dict], list[dict]]:
        eligible = []
        ignored = []
        for trade in trades:
            if self._is_before_start(trade["created_at"], copy_start_at):
                ignored.append(trade)
            else:
                eligible.append(trade)
        return eligible, ignored

    def _mark_prestart_trades(self, trades: list[dict], copy_start_at: str) -> None:
        for trade in trades:
            database.mark_source_trade(
                trade["source_trade_id"],
                "ignored",
                last_error=f"Trade happened before copy start {copy_start_at}.",
                db_path=self.db_path,
            )

    def _is_before_start(self, created_at: str, copy_start_at: str) -> bool:
        if not copy_start_at:
            return False
        try:
            trade_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            start_time = datetime.fromisoformat(copy_start_at.replace("Z", "+00:00"))
            return trade_time <= start_time
        except ValueError:
            return False
