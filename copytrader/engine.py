from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import database
from .config import DB_PATH
from .polymarket import PolymarketClient

MIN_BET_USD = 0.05
MIN_SETTLEMENT_USD = 0.01
MEANINGFUL_MIN_BET_USD = 1.00
MIN_CASH_RESERVE_PCT = 0.20
BASE_SINGLE_BET_CASH_PCT = 0.20
MAX_TRADE_FETCH_LIMIT = 10
SOURCE_POSITION_FETCH_LIMIT = 200
BUY_REPLAY_GUARD_SECONDS = 15
FRESH_FILL_MARK_GRACE_SECONDS = 5
FULLY_PRICED_EXIT_THRESHOLD = 0.98
RESOLVED_LOSS_PRICE_THRESHOLD = 0.001
RESOLVED_SWEEP_MAX_POSITIONS = 120
PORTFOLIO_GROWTH_STEP_PCT = 0.50
PORTFOLIO_GROWTH_STABILITY_SECONDS = 15
HIGH_CONVICTION_TRADE_USD = 1000.0
MAX_BUY_EXPIRY_HOURS = 24
POSITION_CAP_ACTIVATION_EQUITY_USD = 2000.0
POSITION_CAP_BASE_USD = 400.0
POSITION_CAP_STEP_EQUITY_USD = 2500.0
POSITION_CAP_STEP_USD = 400.0
TITLE_STOPWORDS = {
    "vs", "will", "win", "on", "the", "and", "for", "set", "game", "games",
    "totals", "total", "winner", "match", "team", "over", "under", "draw",
    "ou", "yes", "no", "to",
}
MONTH_NAME_TO_NUMBER = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
TIMEZONE_OFFSETS = {
    "utc": timezone.utc,
    "gmt": timezone.utc,
    "et": timezone(timedelta(hours=-5)),
    "est": timezone(timedelta(hours=-5)),
    "edt": timezone(timedelta(hours=-4)),
    "ct": timezone(timedelta(hours=-6)),
    "cst": timezone(timedelta(hours=-6)),
    "cdt": timezone(timedelta(hours=-5)),
    "mt": timezone(timedelta(hours=-7)),
    "mst": timezone(timedelta(hours=-7)),
    "mdt": timezone(timedelta(hours=-6)),
    "pt": timezone(timedelta(hours=-8)),
    "pst": timezone(timedelta(hours=-8)),
    "pdt": timezone(timedelta(hours=-7)),
}


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


def _extract_market_date(value: str) -> date | None:
    if not value:
        return None
    match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _market_effective_date(record: dict) -> date | None:
    return _extract_market_date(record.get("market_slug") or "") or _extract_market_date(record.get("market_title") or "")


def _end_of_day_utc(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=timezone.utc)


def _extract_market_expiry(value: str, now: datetime | None = None) -> datetime | None:
    if not value:
        return None

    now = now or datetime.now(timezone.utc)
    text = str(value).strip()

    iso_datetime_match = re.search(r"(\d{4}-\d{2}-\d{2}[ t]\d{1,2}:\d{2}(?::\d{2})?(?:z|[+-]\d{2}:?\d{2})?)", text, re.IGNORECASE)
    if iso_datetime_match:
        parsed = _parse_iso(iso_datetime_match.group(1).upper().replace(" ", "T"))
        if parsed:
            return parsed

    month_day_match = re.search(
        r"\b("
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
        r")\s+(\d{1,2})(?:,\s*(\d{4}))?"
        r"(?:\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(utc|gmt|et|est|edt|ct|cst|cdt|mt|mst|mdt|pt|pst|pdt)?\b",
        text,
        re.IGNORECASE,
    )
    if month_day_match:
        month = MONTH_NAME_TO_NUMBER[month_day_match.group(1).lower()]
        day = int(month_day_match.group(2))
        year = int(month_day_match.group(3) or now.year)
        hour = int(month_day_match.group(4))
        minute = int(month_day_match.group(5) or 0)
        ampm = (month_day_match.group(6) or "").lower()
        tz_name = (month_day_match.group(7) or "utc").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        try:
            parsed = datetime(year, month, day, hour, minute, tzinfo=TIMEZONE_OFFSETS.get(tz_name, timezone.utc))
        except ValueError:
            return None
        if not month_day_match.group(3) and parsed.astimezone(timezone.utc) < now - timedelta(days=180):
            parsed = parsed.replace(year=parsed.year + 1)
        return parsed.astimezone(timezone.utc)

    month_date_match = re.search(
        r"\b("
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
        r")\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
        text,
        re.IGNORECASE,
    )
    if month_date_match:
        month = MONTH_NAME_TO_NUMBER[month_date_match.group(1).lower()]
        day = int(month_date_match.group(2))
        year = int(month_date_match.group(3) or now.year)
        try:
            parsed_date = date(year, month, day)
        except ValueError:
            return None
        if not month_date_match.group(3) and parsed_date < now.date() - timedelta(days=180):
            parsed_date = date(parsed_date.year + 1, parsed_date.month, parsed_date.day)
        return _end_of_day_utc(parsed_date)

    numeric_date_match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if numeric_date_match:
        month = int(numeric_date_match.group(1))
        day = int(numeric_date_match.group(2))
        year_text = numeric_date_match.group(3)
        year = int(year_text) if year_text else now.year
        if year_text and len(year_text) == 2:
            year += 2000
        try:
            parsed_date = date(year, month, day)
        except ValueError:
            return None
        if not year_text and parsed_date < now.date() - timedelta(days=180):
            parsed_date = date(parsed_date.year + 1, parsed_date.month, parsed_date.day)
        return _end_of_day_utc(parsed_date)

    parsed_date = _extract_market_date(text)
    if parsed_date:
        return _end_of_day_utc(parsed_date)
    return None


def _trade_expiry_from_title(trade: dict, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    payload = _record_payload(trade)
    for candidate in (
        trade.get("market_title"),
        payload.get("market_title"),
        payload.get("marketTitle"),
        payload.get("title"),
        trade.get("market_slug"),
        payload.get("market_slug"),
        payload.get("marketSlug"),
        payload.get("slug"),
    ):
        expiry = _extract_market_expiry(str(candidate or ""), now=now)
        if expiry:
            return expiry
    return None


def _buy_expiry_guard_reason(trade: dict, now: datetime | None = None) -> str | None:
    now = now or datetime.now(timezone.utc)
    expiry = _trade_expiry_from_title(trade, now=now)
    if not expiry:
        return None
    hours_until_expiry = (expiry - now).total_seconds() / 3600.0
    if hours_until_expiry > MAX_BUY_EXPIRY_HOURS:
        return (
            f"Market expires too far out ({hours_until_expiry:.1f}h > {MAX_BUY_EXPIRY_HOURS}h) "
            f"based on title-derived expiry {expiry.isoformat()}."
        )
    return None


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


def _copy_trade_size(
    local_equity: float,
    source_amount_usd: float,
    leader_wallet_value: float,
    buying_capacity: float,
    cash_balance: float,
    correlation_strength: float = 0.0,
) -> float:
    local_equity = max(float(local_equity), 0.0)
    source_amount_usd = max(float(source_amount_usd), 0.0)
    leader_wallet_value = max(float(leader_wallet_value), 0.0)
    buying_capacity = max(float(buying_capacity), 0.0)
    cash_balance = max(float(cash_balance), 0.0)
    if local_equity <= 0:
        return 0.0

    if buying_capacity <= 0:
        return 0.0

    if source_amount_usd <= 0 or leader_wallet_value <= 0:
        return buying_capacity

    aggression_fraction = _clamp(source_amount_usd / leader_wallet_value, 0.0, 1.0)
    amplified_fraction = aggression_fraction**0.65 if aggression_fraction > 0 else 0.0
    scaled_amount = _round_up_to_cent(local_equity * amplified_fraction)
    source_signal_strength = _clamp(source_amount_usd / HIGH_CONVICTION_TRADE_USD, 0.0, 1.0)
    conviction_floor_fraction = max(source_signal_strength * 0.35, correlation_strength * 0.55)
    conviction_floor = _round_up_to_cent(buying_capacity * conviction_floor_fraction)
    if scaled_amount >= MEANINGFUL_MIN_BET_USD:
        return min(max(scaled_amount, conviction_floor), buying_capacity)

    if source_signal_strength >= 0.5 or correlation_strength >= 0.5:
        return buying_capacity
    return min(max(conviction_floor, 0.0), buying_capacity)


def _position_value_cap(local_equity: float) -> float | None:
    equity = max(float(local_equity), 0.0)
    if equity <= POSITION_CAP_ACTIVATION_EQUITY_USD:
        return None
    increments = math.floor((equity - POSITION_CAP_ACTIVATION_EQUITY_USD) / POSITION_CAP_STEP_EQUITY_USD)
    return round(POSITION_CAP_BASE_USD + (max(increments, 0) * POSITION_CAP_STEP_USD), 2)


def _current_position_value(
    position_key: str,
    source_positions: list[dict],
    *,
    fallback_price: float = 0.0,
    db_path=DB_PATH,
) -> float:
    position = database.get_local_position(position_key, db_path)
    if not position:
        return 0.0

    shares = float(position.get("shares") or 0.0)
    stored_value = round(float(position.get("notional_usd") or 0.0), 2)
    if shares <= 0:
        return max(stored_value, 0.0)

    market_slug, _, outcome = position_key.partition(":")
    current_price = 0.0
    for row in source_positions or []:
        if (row.get("market_slug") or "") == market_slug and (row.get("outcome") or "") == outcome:
            current_price = float(row.get("price") or 0.0)
            break

    if current_price <= 0:
        current_price = max(float(position.get("avg_price") or 0.0), float(fallback_price or 0.0))
    live_value = round(shares * current_price, 2) if current_price > 0 else 0.0
    return max(stored_value, live_value, 0.0)


def _title_tokens(value: str) -> set[str]:
    tokens = set()
    for token in re.split(r"[^a-z0-9]+", (value or "").strip().lower()):
        if len(token) < 3 or token in TITLE_STOPWORDS or token.isdigit():
            continue
        tokens.add(token)
    return tokens


def _normalized_market_title(record: dict) -> str:
    payload = _record_payload(record)
    return _normalize_text(
        record.get("market_title") or payload.get("market_title") or payload.get("marketTitle") or payload.get("title") or ""
    )


def _normalized_outcome_name(record: dict) -> str:
    payload = _record_payload(record)
    return _normalize_text(record.get("outcome") or payload.get("outcome") or payload.get("outcomeName") or "")


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
        position_key = source_trade.get("position_key") or f"{source_trade.get('market_slug')}:{source_trade.get('outcome')}"
        shares = round(requested_amount_usd / executed_price, 6)
        actual_amount_usd = round(requested_amount_usd, 2)
        if side == "SELL":
            position = database.get_local_position(position_key, db_path)
            if not position or float(position.get("shares") or 0.0) <= 0:
                raise RuntimeError("No local position available to sell")
            shares = round(min(float(position.get("shares") or 0.0), shares), 6)
            actual_amount_usd = round(shares * executed_price, 2)
        order = {
            "order_id": str(uuid.uuid4()),
            "source_trade_id": source_trade["source_trade_id"],
            "market_slug": source_trade["market_slug"],
            "market_title": source_trade.get("market_title"),
            "outcome": source_trade["outcome"],
            "side": side,
            "requested_amount_usd": actual_amount_usd,
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
        position_key = f"{market_slug}:{outcome}"
        if side == "SELL" and requested_amount_usd <= 0 and source_price <= 0:
            executed_price = 0.0
            position = database.get_local_position(position_key, db_path)
            shares = round(float(position["shares"]) if position else 0.0, 6)
            actual_amount_usd = 0.0
        else:
            source_price = max(source_price, 0.01)
            executed_price = source_price * (1 + slippage) if side == "BUY" else source_price * (1 - slippage)
            executed_price = _clamp(executed_price, 0.01, 0.99)
            shares = round(requested_amount_usd / executed_price, 6)
            actual_amount_usd = requested_amount_usd
            if side == "SELL":
                position = database.get_local_position(position_key, db_path)
                if not position or float(position.get("shares") or 0.0) <= 0:
                    raise RuntimeError("No local position available to sell")
                shares = round(min(float(position.get("shares") or 0.0), shares), 6)
                actual_amount_usd = round(shares * executed_price, 2)
        order = {
            "order_id": str(uuid.uuid4()),
            "source_trade_id": None,
            "market_slug": market_slug,
            "market_title": market_title,
            "outcome": outcome,
            "side": side,
            "requested_amount_usd": actual_amount_usd,
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
        position_key = order.get("position_key") or f"{order['market_slug']}:{order['outcome']}"
        position = database.get_local_position(position_key, db_path)
        cost = round(order["requested_amount_usd"], 2)

        if order["side"] == "BUY":
            database.reserve_paper_cash(cost, db_path)
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
        database.credit_paper_cash(proceeds, db_path)


class ShadowBroker:
    def __init__(self, client: PolymarketClient):
        self.client = client

    def _shadow_position(self, source_trade: dict, db_path=DB_PATH) -> dict | None:
        position_key = source_trade.get("position_key") or f"{source_trade.get('market_slug')}:{source_trade.get('outcome')}"
        return next(
            (row for row in database.shadow_trade_analytics(db_path)["open_positions"] if row.get("position_key") == position_key),
            None,
        )

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

    def preview(self, source_trade: dict, requested_amount_usd: float, settings: dict, db_path=DB_PATH) -> dict:
        side = source_trade["side"]
        source_price = max(float(source_trade.get("price") or 0.0), 0.01)
        paper_slippage = float(settings["slippage_bps"]) / 10000.0
        extra_live_slippage = float(settings.get("shadow_extra_slippage_bps") or 0.0) / 10000.0
        paper_price = source_price * (1 + paper_slippage) if side == "BUY" else source_price * (1 - paper_slippage)
        live_price = source_price * (1 + paper_slippage + extra_live_slippage) if side == "BUY" else source_price * (1 - paper_slippage - extra_live_slippage)
        paper_price = round(_clamp(paper_price, 0.01, 0.99), 4)
        live_price = round(_clamp(live_price, 0.01, 0.99), 4)
        estimated_live_shares = round(requested_amount_usd / live_price, 6) if live_price > 0 else 0.0
        liquidation_position = None
        if side == "SELL":
            liquidation_position = self._shadow_position(source_trade, db_path)
            if liquidation_position and float(liquidation_position.get("shares") or 0.0) > 0:
                estimated_live_shares = round(
                    min(
                        float(liquidation_position.get("shares") or 0.0),
                        estimated_live_shares if estimated_live_shares > 0 else float(liquidation_position.get("shares") or 0.0),
                    ),
                    6,
                )
                if float(source_trade.get("price") or 0.0) <= RESOLVED_LOSS_PRICE_THRESHOLD:
                    paper_price = 0.0
                    live_price = 0.0
                    requested_amount_usd = 0.0
                else:
                    requested_amount_usd = round(estimated_live_shares * live_price, 2)
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
        if side == "SELL" and liquidation_position and float(source_trade.get("price") or 0.0) <= RESOLVED_LOSS_PRICE_THRESHOLD:
            effective_live_price = 0.0
        else:
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
        self._clob_client = None
        self._clob_client_key = None

    def _wallet_address(self, settings: dict) -> str:
        return (settings.get("live_wallet_address") or "").strip()

    def _ensure_live_configuration(self, settings: dict) -> str:
        wallet_address = self._wallet_address(settings)
        if not wallet_address:
            raise RuntimeError("Live execution mode requires a configured live wallet address.")
        return wallet_address

    def _record_live_intent(self, order: dict, db_path=DB_PATH) -> None:
        database.insert_live_order_attempt(order, db_path)
        database.set_app_state("live_last_intent_at", order["created_at"], db_path)
        database.set_app_state("live_last_intent_status", order["status"], db_path)
        database.set_app_state("live_last_intent_error", order.get("failure_reason") or "", db_path)

    def _clob_configuration(self, settings: dict) -> dict:
        private_key = (os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
        if not private_key:
            raise RuntimeError("Missing POLYMARKET_PRIVATE_KEY for live trading.")
        wallet_address = self._ensure_live_configuration(settings)
        host = (settings.get("live_api_base_url") or "https://clob.polymarket.com").strip().rstrip("/")
        chain_id = int((os.environ.get("POLYMARKET_CHAIN_ID") or "137").strip() or "137")
        configured_funder = (os.environ.get("POLYMARKET_FUNDER") or wallet_address).strip()
        configured_signature_type = (os.environ.get("POLYMARKET_SIGNATURE_TYPE") or "").strip()
        return {
            "host": host,
            "chain_id": chain_id,
            "private_key": private_key,
            "wallet_address": wallet_address,
            "funder": configured_funder,
            "signature_type": int(configured_signature_type) if configured_signature_type else None,
            "api_key": (os.environ.get("POLYMARKET_API_KEY") or "").strip(),
            "api_secret": (os.environ.get("POLYMARKET_API_SECRET") or "").strip(),
            "api_passphrase": (os.environ.get("POLYMARKET_API_PASSPHRASE") or "").strip(),
        }

    def _live_client(self, settings: dict):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as exc:
            raise RuntimeError("py-clob-client is required for live trading.") from exc

        config = self._clob_configuration(settings)
        cache_key = (
            config["host"],
            config["chain_id"],
            config["wallet_address"],
            config["funder"],
            config["signature_type"],
            config["api_key"],
            config["api_secret"],
            config["api_passphrase"],
        )
        if self._clob_client is not None and self._clob_client_key == cache_key:
            return self._clob_client

        base_client = ClobClient(
            config["host"],
            chain_id=config["chain_id"],
            key=config["private_key"],
            signature_type=config["signature_type"],
            funder=config["funder"],
        )
        creds = None
        if config["api_key"] and config["api_secret"] and config["api_passphrase"]:
            creds = ApiCreds(config["api_key"], config["api_secret"], config["api_passphrase"])
        else:
            creds = base_client.create_or_derive_api_creds()
        base_client.set_api_creds(creds)
        self._clob_client = base_client
        self._clob_client_key = cache_key
        return base_client

    def refresh_account_snapshot(self, settings: dict, db_path=DB_PATH) -> dict:
        wallet_address = self._ensure_live_configuration(settings)
        positions = self.client.fetch_positions(wallet_address, limit=200)
        gross_exposure = round(sum(float(item.get("notional_usd") or 0.0) for item in positions), 2)
        cash_balance = 0.0
        live_error = ""
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            live_client = self._live_client(settings)
            balance = live_client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            if isinstance(balance, dict):
                cash_balance = round(float(balance.get("balance") or balance.get("available_balance") or 0.0), 2)
        except Exception as exc:
            live_error = str(exc)
        snapshot = {
            "wallet_address": wallet_address,
            "cash_balance": cash_balance,
            "gross_exposure": gross_exposure,
            "net_value": round(cash_balance + gross_exposure, 2),
            "positions_count": len(positions),
            "positions": positions,
            "error": live_error,
        }
        database.snapshot_live_account(
            wallet_address,
            snapshot["cash_balance"],
            snapshot["gross_exposure"],
            snapshot["net_value"],
            snapshot["positions_count"],
            snapshot,
            db_path,
        )
        return snapshot

    def _resolve_live_position(self, settings: dict, market_slug: str, outcome: str, db_path=DB_PATH) -> dict | None:
        snapshot = self.refresh_account_snapshot(settings, db_path)
        for position in snapshot.get("positions") or []:
            if (position.get("market_slug") or "") == (market_slug or "") and (position.get("outcome") or "") == (outcome or ""):
                return position
        return None

    def execute(self, source_trade: dict, requested_amount_usd: float, settings: dict, db_path=DB_PATH) -> dict:
        wallet_address = self._ensure_live_configuration(settings)
        live_client = self._live_client(settings)
        if not bool(int(settings.get("live_trading_enabled") or 0)):
            raise RuntimeError("Live trading is disabled in settings.")
        intent = self.client.build_live_order_intent(
            market_slug=source_trade.get("market_slug") or "",
            outcome=source_trade.get("outcome") or "",
            side=source_trade.get("side") or "",
            requested_amount_usd=requested_amount_usd,
            price_buffer_bps=float(settings.get("live_price_buffer_bps") or 0.0),
        )
        max_order_usd = max(float(settings.get("live_max_order_usd") or 0.0), 0.0)
        if max_order_usd > 0 and float(intent["requested_amount_usd"]) > max_order_usd + 1e-9:
            raise RuntimeError(f"Requested live order exceeds configured max of ${max_order_usd:.2f}.")
        side = source_trade.get("side") or ""
        live_order_payload = None
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            if side == "SELL":
                sell_shares = float(source_trade.get("shares") or 0.0)
                if sell_shares <= 0:
                    live_position = self._resolve_live_position(settings, source_trade.get("market_slug") or "", source_trade.get("outcome") or "", db_path)
                    sell_shares = float((live_position or {}).get("shares") or 0.0)
                if sell_shares <= 0:
                    sell_shares = float(intent.get("estimated_shares") or 0.0)
                if sell_shares <= 0:
                    raise RuntimeError("Missing live sell size.")
                market_args = MarketOrderArgs(
                    token_id=intent["token_id"],
                    amount=round(sell_shares, 6),
                    side=side,
                    price=float(intent["limit_price"]),
                    order_type=OrderType.FOK,
                )
            else:
                market_args = MarketOrderArgs(
                    token_id=intent["token_id"],
                    amount=float(intent["requested_amount_usd"]),
                    side=side,
                    price=float(intent["limit_price"]),
                    order_type=OrderType.FOK,
                )
            signed_order = live_client.create_market_order(market_args)
            live_order_payload = live_client.post_order(signed_order, OrderType.FOK)
            payload_success = bool((live_order_payload or {}).get("success"))
            payload_status = str((live_order_payload or {}).get("status") or "").strip().upper()
            status = "SUBMITTED" if payload_success else (payload_status or "SUBMITTED")
            failure_reason = ""
        except Exception as exc:
            status = "FAILED"
            failure_reason = str(exc)
        order = {
            "live_order_id": str(uuid.uuid4()),
            "source_trade_id": source_trade.get("source_trade_id"),
            "market_slug": intent["market_slug"],
            "market_title": source_trade.get("market_title") or intent["market_title"],
            "outcome": intent["outcome"],
            "side": intent["side"],
            "requested_amount_usd": intent["requested_amount_usd"],
            "limit_price": intent["limit_price"],
            "estimated_shares": intent["estimated_shares"],
            "token_id": intent["token_id"],
            "wallet_address": wallet_address,
            "status": status,
            "failure_reason": failure_reason,
            "created_at": database.utc_now(),
            "position_key": source_trade.get("position_key", ""),
            "match_strategy": source_trade.get("match_strategy", ""),
            "reference_price": intent["reference_price"],
            "book_price": intent["book_price"],
            "price_buffer_bps": intent["price_buffer_bps"],
            "submission_enabled": True,
            "raw_response": live_order_payload or {},
        }
        self._record_live_intent(order, db_path)
        self.refresh_account_snapshot(settings, db_path)
        if status == "FAILED":
            raise RuntimeError(failure_reason)
        return {
            "order_id": order["live_order_id"],
            "live_order_id": order["live_order_id"],
            "source_trade_id": order.get("source_trade_id"),
            "market_slug": order["market_slug"],
            "market_title": order.get("market_title"),
            "outcome": order["outcome"],
            "side": order["side"],
            "requested_amount_usd": order["requested_amount_usd"],
            "executed_price": order["limit_price"],
            "shares": order["estimated_shares"],
            "status": "LIVE_SUBMITTED",
            "failure_reason": None,
            "created_at": order["created_at"],
            "position_key": order.get("position_key", ""),
            "match_strategy": order.get("match_strategy", ""),
            "raw_json": json.dumps(order.get("raw_response") or {}),
        }

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
        trade = {
            "source_trade_id": None,
            "market_slug": market_slug,
            "market_title": market_title,
            "outcome": outcome,
            "side": side,
            "price": price,
            "position_key": "",
            "match_strategy": "manual-live-intent",
            "reason": reason,
        }
        if side == "SELL":
            live_position = self._resolve_live_position(settings, market_slug, outcome, db_path)
            if live_position:
                trade["shares"] = float(live_position.get("shares") or 0.0)
                if requested_amount_usd <= 0:
                    requested_amount_usd = round(float(live_position.get("notional_usd") or 0.0), 2)
        return self.execute(trade, requested_amount_usd, settings, db_path)


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
        self._last_resolved_sweep_at = ""

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
            if self._execution_mode(settings) == "live":
                self.live_broker.refresh_account_snapshot(settings, self.db_path)
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
            synthetic_sell_trades = []
            if self._position_delta_sells_enabled(settings):
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
            if self._autonomous_sells_enabled(settings):
                resolved_exits = self._reconcile_resolved_positions(settings)
                copied += resolved_exits
                if resolved_exits:
                    database.refresh_local_position_market_values(self.db_path, source_positions=positions)
                shadow_resolved_exits = self._reconcile_resolved_shadow_positions(settings)
                copied += shadow_resolved_exits
                auto_zero_value_exits = self._liquidate_zero_value_positions(settings, positions)
                copied += auto_zero_value_exits
                if auto_zero_value_exits:
                    database.refresh_local_position_market_values(self.db_path, source_positions=positions)
                milestone_liquidations = self._liquidate_positions_at_growth_milestone(settings, positions)
                copied += milestone_liquidations
                if milestone_liquidations:
                    database.refresh_local_position_market_values(self.db_path, source_positions=positions)
                auto_liquidated = self._liquidate_fully_priced_positions(settings, positions)
                copied += auto_liquidated
                if auto_liquidated:
                    database.refresh_local_position_market_values(self.db_path, source_positions=positions)
            reconciliation_mismatches = self._reconcile_source_sells(
                settings=settings,
                previous_local_positions=previous_local_positions,
                current_local_positions=database.get_local_positions(self.db_path),
                source_sell_trades=all_seen_trades,
                source_positions=positions,
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

    def _reconcile_resolved_positions(self, settings: dict) -> int:
        now = datetime.now(timezone.utc)
        last_sweep_at = _parse_iso(self._last_resolved_sweep_at)
        if last_sweep_at and (now - last_sweep_at).total_seconds() < 30:
            return 0

        open_positions = database.get_local_positions(self.db_path)
        if not open_positions:
            self._last_resolved_sweep_at = database.utc_now()
            return 0

        candidate_rows = []
        today = now.date()
        for row in open_positions:
            if float(row.get("shares") or 0.0) <= 0:
                continue
            market_date = _market_effective_date(row)
            if market_date is None or market_date > today:
                continue
            candidate_rows.append(
                {
                    "position_key": row.get("position_key"),
                    "market_slug": row.get("market_slug"),
                    "market_title": row.get("market_title"),
                    "outcome": row.get("outcome"),
                    "updated_at": row.get("updated_at") or "",
                }
            )

        if not candidate_rows:
            self._last_resolved_sweep_at = database.utc_now()
            return 0

        candidate_rows.sort(key=lambda row: row["updated_at"])
        candidate_rows = candidate_rows[:RESOLVED_SWEEP_MAX_POSITIONS]
        market_prices = database.fetch_live_market_prices(candidate_rows, self.db_path)
        if not market_prices:
            self._last_resolved_sweep_at = database.utc_now()
            return 0

        marked_positions = {
            row["position_key"]: row
            for row in database.list_local_positions_marked(self.db_path, source_positions=market_prices)
        }
        closed = 0
        for candidate in candidate_rows:
            position = marked_positions.get(candidate["position_key"])
            if not position or float(position.get("shares") or 0.0) <= 0:
                continue
            current_price = float(position.get("current_price") or 0.0)
            if current_price <= RESOLVED_LOSS_PRICE_THRESHOLD:
                self._execute_manual_trade(
                    market_slug=position["market_slug"],
                    market_title=position.get("market_title"),
                    outcome=position["outcome"],
                    side="SELL",
                    price=0.0,
                    requested_amount_usd=0.0,
                    reason="Resolved market sweep closed losing position at 0c.",
                    settings=settings,
                    match_strategy="resolved-market-sweep-loss",
                )
                closed += 1
                continue
            if current_price > FULLY_PRICED_EXIT_THRESHOLD:
                self._execute_manual_trade(
                    market_slug=position["market_slug"],
                    market_title=position.get("market_title"),
                    outcome=position["outcome"],
                    side="SELL",
                    price=current_price,
                    requested_amount_usd=round(float(position.get("market_value") or 0.0), 2),
                    reason="Resolved market sweep closed winning position above 98c.",
                    settings=settings,
                    match_strategy="resolved-market-sweep-win",
                )
                closed += 1

        self._last_resolved_sweep_at = database.utc_now()
        if closed:
            database.log(
                "INFO",
                "reconcile",
                "Resolved market sweep closed settled positions.",
                {"positions_liquidated": closed, "candidates_scanned": len(candidate_rows)},
                self.db_path,
            )
        return closed

    def _reconcile_resolved_shadow_positions(self, settings: dict) -> int:
        if self._execution_mode(settings) != "shadow":
            return 0

        now = datetime.now(timezone.utc)
        shadow_positions = database.shadow_trade_analytics(self.db_path)["open_positions"]
        if not shadow_positions:
            return 0

        candidate_rows = []
        today = now.date()
        for row in shadow_positions:
            if float(row.get("shares") or 0.0) <= 0:
                continue
            market_date = _market_effective_date(row)
            if market_date is None or market_date > today:
                continue
            candidate_rows.append(
                {
                    "position_key": row.get("position_key"),
                    "market_slug": row.get("market_slug"),
                    "market_title": row.get("market_title"),
                    "outcome": row.get("outcome"),
                    "shares": round(float(row.get("shares") or 0.0), 6),
                    "updated_at": row.get("updated_at") or row.get("entry_time") or "",
                }
            )

        if not candidate_rows:
            return 0

        candidate_rows.sort(key=lambda row: row["updated_at"])
        candidate_rows = candidate_rows[:RESOLVED_SWEEP_MAX_POSITIONS]
        market_prices = database.fetch_live_market_prices(candidate_rows, self.db_path)
        if not market_prices:
            return 0

        price_by_key = {
            row["position_key"]: float(row.get("price") or 0.0)
            for row in market_prices
            if row.get("position_key")
        }
        closed = 0
        for candidate in candidate_rows:
            current_price = price_by_key.get(candidate["position_key"])
            if current_price is None:
                continue
            if current_price > RESOLVED_LOSS_PRICE_THRESHOLD and current_price <= FULLY_PRICED_EXIT_THRESHOLD:
                continue
            requested_amount_usd = 0.0 if current_price <= RESOLVED_LOSS_PRICE_THRESHOLD else round(candidate["shares"] * current_price, 2)
            database.insert_shadow_order(
                {
                    "shadow_order_id": str(uuid.uuid4()),
                    "source_trade_id": None,
                    "market_slug": candidate["market_slug"],
                    "market_title": candidate.get("market_title"),
                    "outcome": candidate["outcome"],
                    "side": "SELL",
                    "requested_amount_usd": requested_amount_usd,
                    "reference_price": round(current_price, 4),
                    "paper_price": round(current_price, 4),
                    "estimated_live_price": round(current_price, 4),
                    "estimated_live_shares": candidate["shares"],
                    "price_delta_bps": 0.0,
                    "price_delta_cents": 0.0,
                    "execution_drag_usd": 0.0,
                    "status": "SHADOW",
                    "created_at": database.utc_now(),
                    "position_key": candidate["position_key"],
                    "match_strategy": (
                        "resolved-shadow-sweep-loss"
                        if current_price <= RESOLVED_LOSS_PRICE_THRESHOLD
                        else "resolved-shadow-sweep-win"
                    ),
                },
                self.db_path,
            )
            closed += 1

        if closed:
            database.log(
                "INFO",
                "reconcile",
                "Resolved market sweep closed settled shadow positions.",
                {"positions_liquidated": closed, "candidates_scanned": len(candidate_rows)},
                self.db_path,
            )
        return closed

    def _execution_mode(self, settings: dict) -> str:
        mode = (settings.get("execution_mode") or "shadow").strip().lower()
        if mode == "live":
            return "live"
        return "shadow"

    def _autonomous_sells_enabled(self, settings: dict) -> bool:
        return bool(int(settings.get("allow_autonomous_sells") or 0))

    def _position_delta_sells_enabled(self, settings: dict) -> bool:
        return bool(int(settings.get("infer_sells_from_position_deltas") or 0))

    def _active_broker(self, settings: dict):
        return self.live_broker if self._execution_mode(settings) == "live" else self.broker

    def _record_shadow_preview(self, trade: dict, requested_amount_usd: float, settings: dict) -> dict | None:
        if self._execution_mode(settings) != "shadow":
            return None
        preview = self.shadow_broker.preview(trade, requested_amount_usd, settings, self.db_path)
        database.insert_shadow_order(preview, self.db_path)
        return preview

    def _execute_copy_trade(self, trade: dict, decision: CopyDecision, settings: dict) -> tuple[dict, dict | None]:
        executable_trade = dict(trade)
        if decision.position_key:
            executable_trade["position_key"] = decision.position_key
        if decision.match_strategy:
            executable_trade["match_strategy"] = decision.match_strategy
        order = self._active_broker(settings).execute(executable_trade, decision.requested_amount_usd, settings, self.db_path)
        if self._execution_mode(settings) != "live":
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
        if self._execution_mode(settings) != "live":
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

    def liquidate_all(self) -> int:
        with self._lock:
            settings = database.get_settings(self.db_path)
            mode = self._execution_mode(settings)
            if mode == "live":
                snapshot = self.live_broker.refresh_account_snapshot(settings, self.db_path)
                positions = snapshot.get("positions") or []
                liquidated = 0
                for position in positions:
                    shares = round(float(position.get("shares") or 0.0), 6)
                    if shares <= 0:
                        continue
                    requested_amount_usd = round(float(position.get("notional_usd") or 0.0), 2)
                    self.live_broker.execute_manual(
                        market_slug=position.get("market_slug") or "",
                        market_title=position.get("market_title"),
                        outcome=position.get("outcome") or "",
                        side="SELL",
                        price=float(position.get("price") or 0.0),
                        requested_amount_usd=requested_amount_usd,
                        reason="Dashboard Liquidate All",
                        settings=settings,
                        db_path=self.db_path,
                    )
                    liquidated += 1
                database.log("INFO", "rebalance", "Liquidate All requested from dashboard.", {"execution_mode": mode, "positions_liquidated": liquidated}, self.db_path)
                return liquidated

            source_positions = database.fetch_live_source_positions(self.db_path)
            positions = [
                row
                for row in database.list_local_positions_marked(self.db_path, source_positions=source_positions)
                if float(row.get("shares") or 0.0) > 0 and float(row.get("market_value") or 0.0) >= MIN_SETTLEMENT_USD
            ]
            liquidated = 0
            for position in positions:
                self._execute_manual_trade(
                    market_slug=position["market_slug"],
                    market_title=position.get("market_title"),
                    outcome=position["outcome"],
                    side="SELL",
                    price=float(position.get("current_price") or 0.0),
                    requested_amount_usd=round(float(position.get("market_value") or 0.0), 2),
                    reason="Dashboard Liquidate All",
                    settings=settings,
                    match_strategy="manual-liquidate-all",
                )
                liquidated += 1
            if liquidated:
                database.refresh_local_position_market_values(self.db_path, source_positions=source_positions)
            database.log("INFO", "rebalance", "Liquidate All requested from dashboard.", {"execution_mode": mode, "positions_liquidated": liquidated}, self.db_path)
            return liquidated

    def liquidate_position(self, position_key: str) -> bool:
        with self._lock:
            clean_key = (position_key or "").strip()
            if not clean_key:
                return False

            settings = database.get_settings(self.db_path)
            mode = self._execution_mode(settings)
            market_slug, _, outcome = clean_key.partition(":")
            if not market_slug or not outcome:
                return False

            if mode == "live":
                snapshot = self.live_broker.refresh_account_snapshot(settings, self.db_path)
                position = next(
                    (
                        row
                        for row in snapshot.get("positions") or []
                        if (row.get("market_slug") or "") == market_slug and (row.get("outcome") or "") == outcome
                    ),
                    None,
                )
                if not position or float(position.get("shares") or 0.0) <= 0:
                    return False
                requested_amount_usd = round(float(position.get("notional_usd") or 0.0), 2)
                self.live_broker.execute_manual(
                    market_slug=market_slug,
                    market_title=position.get("market_title"),
                    outcome=outcome,
                    side="SELL",
                    price=float(position.get("price") or 0.0),
                    requested_amount_usd=requested_amount_usd,
                    reason="Dashboard position sell",
                    settings=settings,
                    db_path=self.db_path,
                )
                database.log(
                    "INFO",
                    "rebalance",
                    "Single-position sell requested from dashboard.",
                    {"execution_mode": mode, "position_key": clean_key},
                    self.db_path,
                )
                return True

            source_positions = database.fetch_live_source_positions(self.db_path)
            position = next(
                (
                    row
                    for row in database.list_local_positions_marked(self.db_path, source_positions=source_positions)
                    if (row.get("position_key") or "") == clean_key and float(row.get("shares") or 0.0) > 0
                ),
                None,
            )
            if not position:
                return False

            current_price = float(position.get("current_price") or 0.0)
            requested_amount_usd = (
                round(float(position.get("market_value") or 0.0), 2)
                if current_price > RESOLVED_LOSS_PRICE_THRESHOLD
                else 0.0
            )
            self._execute_manual_trade(
                market_slug=market_slug,
                market_title=position.get("market_title"),
                outcome=outcome,
                side="SELL",
                price=current_price,
                requested_amount_usd=requested_amount_usd,
                reason="Dashboard single-position sell",
                settings=settings,
                match_strategy="manual-single-position-liquidation",
            )
            database.log(
                "INFO",
                "rebalance",
                "Single-position sell requested from dashboard.",
                {"execution_mode": mode, "position_key": clean_key},
                self.db_path,
            )
            return True

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
        now = datetime.now(timezone.utc)
        correlation_strength = self._buy_correlation_strength(trade, source_positions) if side == "BUY" else 0.0
        single_bet_cash_pct = BASE_SINGLE_BET_CASH_PCT
        if side == "BUY":
            if correlation_strength >= 1.0:
                single_bet_cash_pct = 1.0
            elif correlation_strength >= 0.8:
                single_bet_cash_pct = 0.85
            elif correlation_strength >= 0.45:
                single_bet_cash_pct = 0.65
            elif float(trade.get("amount_usd") or 0.0) >= HIGH_CONVICTION_TRADE_USD:
                single_bet_cash_pct = 0.60
        buying_capacity = min(float(portfolio["cash_balance"]), _round_up_to_cent(cash_balance * single_bet_cash_pct))
        requested_amount = _copy_trade_size(
            local_equity,
            trade.get("amount_usd") or 0.0,
            leader_wallet_value,
            buying_capacity,
            cash_balance,
            correlation_strength=correlation_strength,
        )

        if side == "SELL" and not int(settings["copy_sells"]):
            return CopyDecision("skip", "Sell copying disabled.")

        if side == "BUY":
            expiry_guard_reason = _buy_expiry_guard_reason(trade, now=now)
            if expiry_guard_reason:
                return CopyDecision("skip", expiry_guard_reason)
            if buying_capacity < MIN_BET_USD:
                return CopyDecision("skip", "No remaining buying capacity.")
            duplicate_reason = self._same_price_buy_guard(trade, settings)
            if duplicate_reason:
                return CopyDecision("skip", duplicate_reason)
            position_key = trade.get("position_key") or f"{trade.get('market_slug')}:{trade.get('outcome')}"
            position_value_cap = _position_value_cap(local_equity)
            if position_value_cap is not None:
                current_position_value = _current_position_value(
                    position_key,
                    source_positions,
                    fallback_price=float(trade.get("price") or 0.0),
                    db_path=self.db_path,
                )
                remaining_position_capacity = round(position_value_cap - current_position_value, 2)
                if remaining_position_capacity < MIN_BET_USD:
                    return CopyDecision(
                        "skip",
                        (
                            f"Position cap reached for {position_key} "
                            f"(${position_value_cap:.2f} max at current equity)."
                        ),
                    )
                requested_amount = min(requested_amount, remaining_position_capacity)
            requested_amount = max(requested_amount, MIN_BET_USD)
            requested_amount = min(requested_amount, buying_capacity)
            requested_amount = _round_up_to_cent(requested_amount) if requested_amount > 0 else 0.0
            return CopyDecision("copy", "Buy trade eligible.", requested_amount_usd=requested_amount)

        local_position, match_strategy = self._find_matching_local_position(trade)
        if not local_position or float(local_position.get("shares") or 0.0) <= 0:
            return CopyDecision("skip", "No matching local inventory to sell.")
        price = max(float(trade.get("price") or 0.0), 0.01)
        max_sell_notional = round(float(local_position["shares"]) * price, 2)
        if max_sell_notional < MIN_SETTLEMENT_USD:
            return CopyDecision("skip", "Remaining position rounds below one cent.")
        requested_amount = max(requested_amount, MIN_SETTLEMENT_USD)
        requested_amount = min(requested_amount, max_sell_notional)
        requested_amount = _round_up_to_cent(requested_amount) if requested_amount > 0 else 0.0
        return CopyDecision(
            "copy",
            "Sell trade eligible.",
            requested_amount_usd=requested_amount,
            position_key=local_position["position_key"],
            match_strategy=match_strategy,
        )

    def _growth_milestone_target(self, settings: dict, app_state: dict) -> float:
        stored_target = round(float(app_state.get("milestone_liquidation_target") or 0.0), 2)
        if stored_target >= MIN_SETTLEMENT_USD:
            return stored_target
        starting_balance = round(float(settings.get("paper_starting_balance") or 0.0), 2)
        initial_target = round(starting_balance * (1 + PORTFOLIO_GROWTH_STEP_PCT), 2)
        database.set_app_state("milestone_liquidation_target", f"{initial_target:.2f}", self.db_path)
        return initial_target

    def _liquidate_positions_at_growth_milestone(self, settings: dict, source_positions: list[dict]) -> int:
        app_state = database.get_app_state(self.db_path)
        target_value = self._growth_milestone_target(settings, app_state)
        portfolio = database.portfolio_totals(self.db_path, source_positions)
        net_liquidation_value = round(float(portfolio.get("net_value") or 0.0), 2)
        positions = [
            row
            for row in database.list_local_positions_marked(
                self.db_path,
                freeze_recent_seconds=FRESH_FILL_MARK_GRACE_SECONDS,
                source_positions=source_positions,
            )
            if float(row.get("shares") or 0.0) > 0 and float(row.get("market_value") or 0.0) >= MIN_SETTLEMENT_USD
        ]
        if net_liquidation_value + 1e-9 < target_value or not positions:
            if app_state.get("milestone_liquidation_armed_at"):
                database.set_app_state("milestone_liquidation_armed_at", "", self.db_path)
            return 0

        armed_at = _parse_iso(app_state.get("milestone_liquidation_armed_at") or "")
        now = _parse_iso(database.utc_now()) or datetime.now(timezone.utc)
        if armed_at is None:
            database.set_app_state("milestone_liquidation_armed_at", now.replace(microsecond=0).isoformat(), self.db_path)
            database.log(
                "INFO",
                "rebalance",
                "Portfolio growth milestone armed.",
                {"target_value": target_value, "net_liquidation_value": net_liquidation_value, "stability_seconds": PORTFOLIO_GROWTH_STABILITY_SECONDS},
                self.db_path,
            )
            return 0
        if (now - armed_at).total_seconds() < PORTFOLIO_GROWTH_STABILITY_SECONDS:
            return 0

        liquidated = 0
        for position in positions:
            market_value = round(float(position.get("market_value") or 0.0), 2)
            if market_value < MIN_SETTLEMENT_USD:
                continue
            self._execute_manual_trade(
                market_slug=position["market_slug"],
                market_title=position.get("market_title"),
                outcome=position["outcome"],
                side="SELL",
                price=float(position["current_price"]),
                requested_amount_usd=market_value,
                reason=f"Auto-liquidated after net liquidation value reached {net_liquidation_value:.2f} above the {target_value:.2f} growth milestone.",
                settings=settings,
                match_strategy="growth-milestone-liquidation",
            )
            liquidated += 1

        if liquidated:
            next_target = round(target_value * (1 + PORTFOLIO_GROWTH_STEP_PCT), 2)
            database.set_app_state("milestone_liquidation_target", f"{next_target:.2f}", self.db_path)
            database.set_app_state("milestone_liquidation_armed_at", "", self.db_path)
            database.set_app_state("milestone_liquidation_last_triggered_at", now.replace(microsecond=0).isoformat(), self.db_path)
            database.log(
                "INFO",
                "rebalance",
                "Fully liquidated positions at the portfolio growth milestone.",
                {
                    "positions_liquidated": liquidated,
                    "trigger_value": target_value,
                    "net_liquidation_value": net_liquidation_value,
                    "next_target_value": next_target,
                    "stability_seconds": PORTFOLIO_GROWTH_STABILITY_SECONDS,
                },
                self.db_path,
            )
        return liquidated

    def _liquidate_fully_priced_positions(self, settings: dict, source_positions: list[dict]) -> int:
        positions = [
            row
            for row in database.list_local_positions_marked(self.db_path, source_positions=source_positions)
            if float(row.get("shares") or 0.0) > 0
            and float(row.get("current_price") or 0.0) > FULLY_PRICED_EXIT_THRESHOLD
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
                reason="Auto-sold position after price moved above 98c.",
                settings=settings,
                match_strategy="auto-full-price-exit",
            )
            liquidated += 1

        if liquidated:
            database.log(
                "INFO",
                "rebalance",
                "Auto-sold fully priced shadow positions.",
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
                "Auto-closed zero-value shadow positions.",
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
        buying_capacity = remaining_cash
        if buying_capacity < MIN_BET_USD:
            database.set_app_state("bootstrap_positions_done_at", database.utc_now(), self.db_path)
            return 0

        copied = 0
        bootstrap_time = app_state.get("copy_start_at") or database.utc_now()
        position_value_cap = _position_value_cap(float(portfolio["net_value"]))
        ordered_positions = sorted(positions, key=lambda row: float(row.get("notional_usd") or 0.0), reverse=True)
        for position in ordered_positions:
            if buying_capacity < MIN_BET_USD:
                break
            weight = float(position.get("notional_usd") or 0.0) / leader_wallet_value if leader_wallet_value > 0 else 0.0
            if weight <= 0:
                continue
            requested_amount = _round_up_to_cent(float(portfolio["net_value"]) * weight)
            if position_value_cap is not None:
                requested_amount = min(requested_amount, position_value_cap)
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
                "Bootstrapped current source positions into local shadow inventory.",
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

    def _buy_correlation_strength(self, trade: dict, source_positions: list[dict]) -> float:
        open_positions = [
            row
            for row in database.list_local_positions_marked(self.db_path, source_positions=source_positions)
            if float(row.get("shares") or 0.0) > 0
        ]
        if not open_positions:
            return 0.0
        same_position = next(
            (
                row for row in open_positions
                if (row.get("market_slug") or "") == (trade.get("market_slug") or "")
                and (row.get("outcome") or "") == (trade.get("outcome") or "")
            ),
            None,
        )
        if same_position:
            return 1.0
        same_market = next(
            (row for row in open_positions if (row.get("market_slug") or "") == (trade.get("market_slug") or "")),
            None,
        )
        if same_market:
            return 0.8

        trade_tokens = _title_tokens(trade.get("market_title") or trade.get("market_slug") or "")
        if not trade_tokens:
            return 0.0
        best_overlap = 0
        for row in open_positions:
            overlap = len(trade_tokens.intersection(_title_tokens(row.get("market_title") or row.get("market_slug") or "")))
            best_overlap = max(best_overlap, overlap)
        if best_overlap >= 4:
            return 0.7
        if best_overlap >= 3:
            return 0.6
        if best_overlap >= 2:
            return 0.45
        return 0.0

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
            same_name_position = self._find_same_name_position(trade, records)
            if same_name_position:
                return same_name_position, "same-name-fallback"
            return None, ""
        shared_aliases = trade_aliases.intersection(_position_aliases(matched_position))
        if not shared_aliases:
            return matched_position, "alias-fallback"
        matched_alias = sorted(shared_aliases)[0]
        alias_type = matched_alias.split(":", 1)[0]
        return matched_position, f"alias-{alias_type}"

    def _find_same_name_position(self, trade: dict, records: list[dict]) -> dict | None:
        trade_outcome = _normalized_outcome_name(trade)
        trade_title = _normalized_market_title(trade)
        if not trade_outcome or not trade_title:
            return None

        candidates = []
        for record in records:
            if float(record.get("shares") or 0.0) <= 0:
                continue
            if _normalized_outcome_name(record) != trade_outcome:
                continue
            record_title = _normalized_market_title(record)
            if record_title != trade_title or not record_title:
                continue
            candidates.append(record)

        if not candidates:
            return None

        return max(
            candidates,
            key=lambda row: (
                float(row.get("shares") or 0.0),
                float(row.get("notional_usd") or row.get("market_value") or 0.0),
            ),
        )

    def _reconcile_source_sells(
        self,
        *,
        settings: dict,
        previous_local_positions: list[dict],
        current_local_positions: list[dict],
        source_sell_trades: list[dict],
        source_positions: list[dict],
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
            marked_position, _ = self._find_matching_marked_local_position(trade, source_positions)
            current_price = 0.0
            if marked_position:
                current_price = float(marked_position.get("current_price") or 0.0)
            if current_price <= 0:
                current_price = max(
                    float(trade.get("price") or 0.0),
                    float(current_position.get("avg_price") or 0.0) if current_position else 0.0,
                    float(previous_position.get("avg_price") or 0.0),
                )
            market_value = round(float(marked_position.get("market_value") or 0.0), 2) if marked_position else round(current_shares * current_price, 2)
            if current_shares > 0 and market_value >= MIN_SETTLEMENT_USD and current_price > 0:
                try:
                    order, _ = self._execute_manual_trade(
                        market_slug=previous_position.get("market_slug") or trade.get("market_slug") or "",
                        market_title=previous_position.get("market_title") or trade.get("market_title"),
                        outcome=previous_position.get("outcome") or trade.get("outcome") or "",
                        side="SELL",
                        price=current_price,
                        requested_amount_usd=market_value,
                        reason=f"Reconciled stale source sell {trade.get('source_trade_id')}",
                        settings=settings,
                        match_strategy="source-sell-reconcile",
                    )
                    database.mark_source_trade(
                        trade["source_trade_id"],
                        "copied",
                        copied_order_id=order["order_id"],
                        last_error="Reconciled delayed source sell.",
                        db_path=self.db_path,
                    )
                    current_by_key[position_key] = {
                        **(current_position or previous_position),
                        "position_key": position_key,
                        "shares": round(max(current_shares - float(order.get("shares") or 0.0), 0.0), 6),
                    }
                    database.log(
                        "INFO",
                        "reconcile",
                        "Reconciled delayed source sell with manual catch-up fill.",
                        {
                            "source_trade_id": trade.get("source_trade_id"),
                            "market_slug": trade.get("market_slug"),
                            "outcome": trade.get("outcome"),
                            "position_key": position_key,
                            "requested_amount_usd": market_value,
                            "executed_price": round(current_price, 4),
                        },
                        self.db_path,
                    )
                    continue
                except Exception as exc:
                    database.log(
                        "ERROR",
                        "reconcile",
                        "Failed to reconcile delayed source sell with manual catch-up fill.",
                        {
                            "source_trade_id": trade.get("source_trade_id"),
                            "market_slug": trade.get("market_slug"),
                            "outcome": trade.get("outcome"),
                            "position_key": position_key,
                            "market_value": market_value,
                            "current_price": round(current_price, 4),
                            "error": str(exc),
                        },
                        self.db_path,
                    )
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
