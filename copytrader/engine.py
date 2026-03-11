from __future__ import annotations

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
BASE_TRADE_PCT = 0.02
MAX_TRADE_USD = 20.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _round_up_to_cent(value: float) -> float:
    if value <= 0:
        return 0.0
    return math.ceil(value * 100) / 100.0


def _bankroll_bet_size(bankroll: float) -> float:
    bankroll = max(float(bankroll), 0.0)
    if bankroll <= 0:
        return 0.0
    sized_amount = min(bankroll * BASE_TRADE_PCT, MAX_TRADE_USD)
    return max(_round_up_to_cent(sized_amount), MIN_BET_USD)


def _baseline_trade_size(bankroll: float) -> float:
    bankroll = max(float(bankroll), 0.0)
    if bankroll <= 0:
        return 0.0
    return max(_round_up_to_cent(bankroll * 0.01), MEANINGFUL_MIN_BET_USD)


def _copy_trade_size(local_equity: float, source_amount_usd: float, leader_wallet_value: float) -> float:
    local_equity = max(float(local_equity), 0.0)
    source_amount_usd = max(float(source_amount_usd), 0.0)
    leader_wallet_value = max(float(leader_wallet_value), 0.0)
    if local_equity <= 0:
        return 0.0

    baseline = min(_baseline_trade_size(local_equity), _bankroll_bet_size(local_equity))
    if source_amount_usd <= 0 or leader_wallet_value <= 0:
        return max(_bankroll_bet_size(local_equity), baseline)

    # Mirror the leader's aggressiveness as a fraction of their wallet,
    # but keep a meaningful floor so small source trades do not collapse into pennies.
    aggression_fraction = _clamp(source_amount_usd / leader_wallet_value, 0.0, 1.0)
    scaled_amount = _round_up_to_cent(local_equity * aggression_fraction)
    return min(max(scaled_amount, baseline), MAX_TRADE_USD)


@dataclass
class CopyDecision:
    action: str
    reason: str
    requested_amount_usd: float = 0.0
    executed_price: float = 0.0
    shares: float = 0.0


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
        }
        self._apply_fill(order, db_path=db_path)
        return order

    def _apply_fill(self, order: dict, db_path=DB_PATH) -> None:
        settings = database.get_settings(db_path)
        cash = float(settings["paper_cash_balance"])
        position_key = f"{order['market_slug']}:{order['outcome']}"
        position = database.get_local_position(position_key, db_path)
        cost = round(order["requested_amount_usd"], 2)

        if order["side"] == "BUY":
            if cost > cash + 1e-9:
                raise RuntimeError("Insufficient paper cash balance")
            new_cash = round(cash - cost, 2)
            existing_shares = float(position["shares"]) if position else 0.0
            existing_notional = float(position["notional_usd"]) if position else 0.0
            new_shares = existing_shares + order["shares"]
            new_notional = existing_notional + cost
            avg_price = round(new_notional / new_shares, 4) if new_shares else order["executed_price"]
            database.update_local_position(
                position_key,
                {
                    "market_slug": order["market_slug"],
                    "market_title": order.get("market_title"),
                    "outcome": order["outcome"],
                    "side": order["side"],
                    "shares": round(new_shares, 6),
                    "avg_price": avg_price,
                    "notional_usd": round(new_notional, 2),
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


class CopyTradingEngine:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.client = PolymarketClient()
        self.broker = PaperBroker()
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
            trades = self.client.fetch_trades(profile["wallet"], handle=profile["handle"], limit=int(settings["trade_fetch_limit"]))
            positions = self.client.fetch_positions(profile["wallet"], limit=100)
            leader_wallet_value = self._get_leader_wallet_value(settings, profile["wallet"], positions)
            trades_seen = len(trades)
            trade_ids = [trade["source_trade_id"] for trade in trades]
            existing_trade_ids = database.get_existing_source_trade_ids(trade_ids, self.db_path)
            fresh_trades = [trade for trade in trades if trade["source_trade_id"] not in existing_trade_ids]
            eligible_fresh_trades, prestart_trades = self._split_by_copy_start(fresh_trades, app_state.get("copy_start_at", ""))
            new_trades = len(fresh_trades)

            copied, failed = self._copy_trades_first(
                sorted(eligible_fresh_trades, key=lambda item: item["created_at"], reverse=True),
                settings,
                leader_wallet_value,
            )
            database.upsert_source_trades(trades, self.db_path)
            self._finalize_fresh_trade_records(eligible_fresh_trades)
            self._mark_prestart_trades(prestart_trades, app_state.get("copy_start_at", ""))
            database.upsert_source_positions(positions, self.db_path)
            backlog_copied, backlog_failed = self._copy_pending(settings, leader_wallet_value, app_state.get("copy_start_at", ""))
            copied += backlog_copied
            failed += backlog_failed
            portfolio = database.portfolio_totals(self.db_path)
            database.snapshot_portfolio(
                portfolio["cash_balance"],
                portfolio["gross_exposure"],
                portfolio["net_value"],
                portfolio["positions_count"],
                self.db_path,
            )
            database.set_app_state("resolved_target_wallet", profile["wallet"], self.db_path)
            database.set_app_state("leader_wallet_value", f"{leader_wallet_value:.8f}", self.db_path)
            database.set_app_state("leader_wallet_updated_at", database.utc_now(), self.db_path)
            database.set_app_state("last_sync_at", database.utc_now(), self.db_path)
            database.set_app_state("last_sync_message", f"Synced {trades_seen} source trades, copied {copied}.", self.db_path)
            database.set_app_state("last_error", "", self.db_path)
            message = f"Synced {trades_seen} trades, {new_trades} new, {copied} copied."
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
        portfolio = database.portfolio_totals(self.db_path)
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

    def _copy_trades_first(self, trades: list[dict], settings: dict, leader_wallet_value: float) -> tuple[int, int]:
        copied = 0
        failed = 0
        self._latest_results = []
        for trade in trades:
            decision = self._decide_copy_trade(trade, settings, leader_wallet_value)
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
                    order = self.broker.execute(trade, decision.requested_amount_usd, settings, self.db_path)
                    database.insert_copy_order(order, self.db_path)
                    result = {
                        "trade_id": trade["source_trade_id"],
                        "copy_status": "copied",
                        "copied_order_id": order["order_id"],
                        "last_error": None,
                        "log_level": "INFO",
                        "log_message": f"Copied {trade['side']} {trade['market_slug']} {trade['outcome']}.",
                        "log_details": {"amount_usd": order["requested_amount_usd"], "price": order["executed_price"]},
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

    def _copy_pending(self, settings: dict, leader_wallet_value: float, copy_start_at: str) -> tuple[int, int]:
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
            decision = self._decide_copy_trade(trade, settings, leader_wallet_value)
            if decision.action == "skip":
                database.mark_source_trade(trade["source_trade_id"], "skipped", last_error=decision.reason, db_path=self.db_path)
                database.log("INFO", "copy", f"Skipped {trade['source_trade_id']}.", {"reason": decision.reason}, self.db_path)
                continue
            try:
                order = self.broker.execute(trade, decision.requested_amount_usd, settings, self.db_path)
                database.insert_copy_order(order, self.db_path)
                database.mark_source_trade(trade["source_trade_id"], "copied", copied_order_id=order["order_id"], db_path=self.db_path)
                database.log(
                    "INFO",
                    "copy",
                    f"Copied {trade['side']} {trade['market_slug']} {trade['outcome']}.",
                    {"amount_usd": order["requested_amount_usd"], "price": order["executed_price"]},
                    self.db_path,
                )
                copied += 1
            except Exception as exc:
                database.mark_source_trade(trade["source_trade_id"], "failed", last_error=str(exc), db_path=self.db_path)
                failed += 1
                database.log("ERROR", "copy", f"Copy failed for {trade['source_trade_id']}.", {"error": str(exc)}, self.db_path)
        return copied, failed

    def _decide_copy_trade(self, trade: dict, settings: dict, leader_wallet_value: float) -> CopyDecision:
        side = trade["side"]
        portfolio = database.portfolio_totals(self.db_path)
        local_equity = max(float(portfolio["net_value"]), 0.0)
        requested_amount = _copy_trade_size(local_equity, trade.get("amount_usd") or 0.0, leader_wallet_value)

        if side == "SELL" and not int(settings["copy_sells"]):
            return CopyDecision("skip", "Sell copying disabled.")

        if side == "BUY":
            remaining_exposure = round(float(settings["max_total_exposure_usd"]) - portfolio["gross_exposure"], 2)
            buying_capacity = min(float(portfolio["cash_balance"]), remaining_exposure)
            if buying_capacity < MIN_BET_USD:
                return CopyDecision("skip", "No remaining buying capacity.")
            requested_amount = max(requested_amount, MIN_BET_USD)
            requested_amount = min(requested_amount, buying_capacity)
            requested_amount = _round_up_to_cent(requested_amount) if requested_amount > 0 else 0.0
            return CopyDecision("copy", "Buy trade eligible.", requested_amount_usd=requested_amount)

        position_key = f"{trade['market_slug']}:{trade['outcome']}"
        local_position = database.get_local_position(position_key, self.db_path)
        if not local_position or float(local_position["shares"]) <= 0:
            return CopyDecision("skip", "No matching local inventory to sell.")
        price = max(float(trade.get("price") or 0.0), 0.01)
        max_sell_notional = round(float(local_position["shares"]) * price, 2)
        if max_sell_notional < MIN_BET_USD:
            return CopyDecision("skip", "Remaining position is too small to sell.")
        requested_amount = max(requested_amount, MIN_BET_USD)
        requested_amount = min(requested_amount, max_sell_notional)
        requested_amount = _round_up_to_cent(requested_amount) if requested_amount > 0 else 0.0
        return CopyDecision("copy", "Sell trade eligible.", requested_amount_usd=requested_amount)

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
