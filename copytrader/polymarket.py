from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
import re
import json

from .config import API_TIMEOUT_SECONDS, USER_AGENT


BURST_TRADE_WINDOW_SECONDS = 2


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_market_price(value: Any, default: float = 0.0) -> float:
    price = _to_float(value, default)
    if price <= 0:
        return 0.0
    return round(price, 2)


def _clean_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def _trade_row_id(item: dict, market_slug: str, outcome: str, side: str, created_at: str, index: int) -> str:
    explicit_id = _clean_id(item.get("id") or item.get("tradeID") or item.get("tradeId"))
    if explicit_id:
        return explicit_id

    tx_hash = _clean_id(item.get("transactionHash"))
    token_id = _clean_id(item.get("asset") or item.get("tokenId") or item.get("token_id") or item.get("outcomeTokenId"))
    if tx_hash:
        suffix_parts = [market_slug or "unknown-market", outcome or "UNKNOWN", side or "UNKNOWN"]
        if token_id:
            suffix_parts.append(token_id)
        else:
            suffix_parts.append(str(index))
        return f"{tx_hash}:{':'.join(suffix_parts)}"

    return f"{market_slug}:{outcome}:{side}:{created_at}:{index}"


def _wallet_like(value: str) -> bool:
    return isinstance(value, str) and value.startswith("0x") and len(value) >= 10


def _iso(value: Any) -> str:
    if not value:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat()
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _best_book_price(levels: Any, *, side: str) -> float:
    if not isinstance(levels, list):
        return 0.0
    parsed_levels: list[float] = []
    for level in levels:
        if isinstance(level, dict):
            price = _to_float(level.get("price"), 0.0)
        elif isinstance(level, (list, tuple)) and level:
            price = _to_float(level[0], 0.0)
        else:
            price = 0.0
        if price > 0:
            parsed_levels.append(price)
    if not parsed_levels:
        return 0.0
    return min(parsed_levels) if side == "BUY" else max(parsed_levels)


class PolymarketClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._profile_cache: dict[str, dict] = {}
        self._market_metadata_cache: dict[str, dict] = {}
        self._fee_rate_cache: dict[str, float] = {}

    def _get_json(self, url: str, params: dict | None = None):
        response = self.session.get(url, params=params, timeout=API_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()

    def post_json(self, url: str, payload: Any):
        response = self.session.post(url, json=payload, timeout=API_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()

    def resolve_target_wallet(self, handle_or_wallet: str) -> dict:
        clean = (handle_or_wallet or "").strip().lstrip("@")
        if not clean:
            return {"handle": "", "wallet": ""}
        cached = self._profile_cache.get(clean.lower())
        if cached:
            return dict(cached)
        if _wallet_like(clean):
            result = {"handle": clean, "wallet": clean}
            self._profile_cache[clean.lower()] = dict(result)
            return result

        candidates = [
            ("https://gamma-api.polymarket.com/profiles", {"handle": clean}),
            ("https://gamma-api.polymarket.com/profile", {"handle": clean}),
            ("https://www.polymarket.com/api/profile", {"handle": clean}),
            ("https://www.polymarket.com/api/profiles", {"handle": clean}),
        ]
        for url, params in candidates:
            try:
                payload = self._get_json(url, params=params)
            except Exception:
                continue
            profile = self._extract_profile(payload)
            if profile.get("wallet"):
                result = {"handle": profile.get("handle") or clean, "wallet": profile["wallet"]}
                self._profile_cache[clean.lower()] = dict(result)
                return result

        profile = self._resolve_from_profile_page(clean)
        if profile.get("wallet"):
            self._profile_cache[clean.lower()] = dict(profile)
            return profile

        raise RuntimeError(f"Could not resolve Polymarket profile for {clean}")

    def _resolve_from_profile_page(self, handle: str) -> dict:
        url = f"https://polymarket.com/@{handle}"
        response = self.session.get(url, timeout=API_TIMEOUT_SECONDS)
        response.raise_for_status()
        html = response.text

        wallet_match = re.search(r'"proxyWallet":"(0x[a-fA-F0-9]{40})"', html)
        handle_match = re.search(r'"username":"([^"]+)"', html)
        if wallet_match:
            return {
                "handle": handle_match.group(1) if handle_match else handle,
                "wallet": wallet_match.group(1),
            }
        return {}

    def _extract_profile(self, payload: Any) -> dict:
        if isinstance(payload, list):
            for item in payload:
                profile = self._extract_profile(item)
                if profile.get("wallet"):
                    return profile
            return {}
        if not isinstance(payload, dict):
            return {}
        wallet = (
            payload.get("walletAddress")
            or payload.get("wallet")
            or payload.get("proxyWallet")
            or payload.get("address")
        )
        handle = payload.get("handle") or payload.get("username") or payload.get("name")
        if wallet:
            return {"handle": handle, "wallet": wallet}
        for key in ("profile", "data", "user"):
            if key in payload:
                nested = self._extract_profile(payload[key])
                if nested.get("wallet"):
                    return nested
        return {}

    def fetch_trades(self, wallet: str, handle: str = "", limit: int = 100) -> list[dict]:
        candidates = [
            ("https://data-api.polymarket.com/activity", {"user": wallet, "limit": limit}),
            ("https://data-api.polymarket.com/trades", {"user": wallet, "limit": limit}),
            ("https://gamma-api.polymarket.com/activity", {"user": wallet, "limit": limit}),
        ]
        for url, params in candidates:
            try:
                payload = self._get_json(url, params=params)
            except Exception:
                continue
            trades = self._normalize_trades(payload, wallet=wallet, handle=handle)
            if trades:
                return trades
        return []

    def fetch_positions(self, wallet: str, limit: int = 100) -> list[dict]:
        candidates = [
            ("https://data-api.polymarket.com/positions", {"user": wallet, "limit": limit}),
            ("https://gamma-api.polymarket.com/positions", {"user": wallet, "limit": limit}),
        ]
        for url, params in candidates:
            try:
                payload = self._get_json(url, params=params)
            except Exception:
                continue
            positions = self._normalize_positions(payload)
            if positions:
                return positions
        return []

    def fetch_market_prices(self, records: list[dict]) -> list[dict]:
        by_slug: dict[str, dict] = {}
        for record in records or []:
            slug = (record.get("market_slug") or "").strip()
            outcome = record.get("outcome") or ""
            if slug and outcome:
                by_slug[slug] = record

        prices: list[dict] = []
        for slug in by_slug:
            try:
                payload = self._get_json("https://gamma-api.polymarket.com/markets", params={"slug": slug})
            except Exception:
                continue
            items = payload if isinstance(payload, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                outcomes = _parse_json_list(item.get("outcomes"))
                outcome_prices = _parse_json_list(item.get("outcomePrices"))
                if not outcomes or not outcome_prices:
                    continue
                market_slug = item.get("slug") or slug
                market_title = item.get("question") or item.get("title") or market_slug
                condition_id = _clean_id(item.get("conditionId") or item.get("condition_id"))
                token_ids = _parse_json_list(item.get("clobTokenIds") or item.get("tokenIds"))
                for index, outcome in enumerate(outcomes):
                    if index >= len(outcome_prices):
                        continue
                    token_id = _clean_id(token_ids[index]) if index < len(token_ids) else ""
                    prices.append(
                        {
                            "position_key": f"{market_slug}:{outcome}",
                            "market_slug": market_slug,
                            "market_title": market_title,
                            "outcome": outcome,
                            "price": _round_market_price(outcome_prices[index], 0.0),
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "category": item.get("category") or "",
                            "market_type": item.get("marketType") or "",
                            "sports_market_type": item.get("sportsMarketType") or "",
                            "fees_enabled": bool(item.get("feesEnabled", False)),
                        }
                    )
        return prices

    def fetch_market_metadata(self, slug: str) -> dict:
        clean_slug = (slug or "").strip()
        if not clean_slug:
            return {}
        cached = self._market_metadata_cache.get(clean_slug)
        if cached:
            return dict(cached)
        try:
            payload = self._get_json("https://gamma-api.polymarket.com/markets", params={"slug": clean_slug})
        except Exception:
            return {}
        items = payload if isinstance(payload, list) else []
        if not items:
            return {}
        item = items[0] if isinstance(items[0], dict) else {}
        if not item:
            return {}
        result = {
            "market_slug": item.get("slug") or clean_slug,
            "category": (item.get("category") or "").strip(),
            "market_type": (item.get("marketType") or "").strip(),
            "sports_market_type": (item.get("sportsMarketType") or "").strip(),
            "fees_enabled": bool(item.get("feesEnabled", False)),
            "maker_base_fee": _to_float(item.get("makerBaseFee"), 0.0),
            "taker_base_fee": _to_float(item.get("takerBaseFee"), 0.0),
        }
        self._market_metadata_cache[clean_slug] = dict(result)
        return result

    def fetch_fee_rate_bps(self, token_id: str) -> float:
        clean_token_id = _clean_id(token_id)
        if not clean_token_id:
            return 0.0
        if clean_token_id in self._fee_rate_cache:
            return float(self._fee_rate_cache[clean_token_id])
        try:
            payload = self._get_json("https://clob.polymarket.com/fee-rate", params={"token_id": clean_token_id})
        except Exception:
            return 0.0
        if not isinstance(payload, dict):
            return 0.0
        fee_rate = _to_float(payload.get("fee_rate_bps"), 0.0)
        self._fee_rate_cache[clean_token_id] = fee_rate
        return fee_rate

    def fetch_order_book(self, token_id: str) -> dict:
        clean_token_id = _clean_id(token_id)
        if not clean_token_id:
            return {}
        try:
            payload = self._get_json("https://clob.polymarket.com/book", params={"token_id": clean_token_id})
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def fetch_order_books(self, token_ids: list[str]) -> dict[str, dict]:
        clean_token_ids = [_clean_id(token_id) for token_id in token_ids if _clean_id(token_id)]
        if not clean_token_ids:
            return {}
        try:
            payload = self.post_json(
                "https://clob.polymarket.com/books",
                [{"token_id": token_id} for token_id in clean_token_ids],
            )
        except Exception:
            return {}
        books = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            asset_id = _clean_id(item.get("asset_id") or item.get("token_id"))
            if asset_id:
                books[asset_id] = item
        return books

    def resolve_market_execution_context(self, market_slug: str, outcome: str) -> dict:
        prices = self.fetch_market_prices([{"market_slug": market_slug, "outcome": outcome}])
        match = next(
            (
                row for row in prices
                if (row.get("market_slug") or "") == (market_slug or "")
                and (row.get("outcome") or "") == (outcome or "")
            ),
            None,
        )
        if not match:
            raise RuntimeError(f"Could not resolve token metadata for {market_slug} {outcome}.")
        token_id = _clean_id(match.get("token_id"))
        if not token_id:
            raise RuntimeError(f"Missing token_id for {market_slug} {outcome}.")
        order_book = self.fetch_order_book(token_id)
        return {
            "token_id": token_id,
            "market_slug": match.get("market_slug") or market_slug,
            "market_title": match.get("market_title") or market_slug,
            "outcome": match.get("outcome") or outcome,
            "reference_price": _to_float(match.get("price"), 0.0),
            "order_book": order_book,
        }

    def build_live_order_intent(
        self,
        *,
        market_slug: str,
        outcome: str,
        side: str,
        requested_amount_usd: float,
        price_buffer_bps: float = 0.0,
    ) -> dict:
        context = self.resolve_market_execution_context(market_slug, outcome)
        order_book = context.get("order_book") or {}
        buffer_fraction = max(float(price_buffer_bps), 0.0) / 10000.0
        reference_price = _to_float(context.get("reference_price"), 0.0)
        asks = order_book.get("asks") or []
        bids = order_book.get("bids") or []
        book_price = _best_book_price(asks if side == "BUY" else bids, side=side)
        raw_limit_price = book_price or reference_price or 0.0
        if raw_limit_price <= 0:
            raise RuntimeError(f"Missing executable price for {market_slug} {outcome}.")
        adjusted_price = raw_limit_price * (1 + buffer_fraction) if side == "BUY" else raw_limit_price * (1 - buffer_fraction)
        limit_price = round(min(max(adjusted_price, 0.01), 0.99), 4)
        estimated_shares = round(float(requested_amount_usd) / limit_price, 6) if limit_price > 0 else 0.0
        return {
            "market_slug": context["market_slug"],
            "market_title": context["market_title"],
            "outcome": context["outcome"],
            "token_id": context["token_id"],
            "reference_price": round(reference_price or raw_limit_price, 4),
            "book_price": round(book_price or raw_limit_price, 4),
            "limit_price": limit_price,
            "estimated_shares": estimated_shares,
            "requested_amount_usd": round(float(requested_amount_usd), 2),
            "side": side,
            "price_buffer_bps": float(price_buffer_bps),
            "order_book": order_book,
        }

    def _normalize_trades(self, payload: Any, *, wallet: str, handle: str) -> list[dict]:
        items = payload if isinstance(payload, list) else payload.get("data") or payload.get("items") or payload.get("history") or []
        trades = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            side = (item.get("side") or item.get("action") or item.get("type") or "").lower()
            if "buy" in side:
                side = "BUY"
            elif "sell" in side:
                side = "SELL"
            else:
                continue
            price = _to_float(item.get("price") or item.get("avgPrice") or item.get("outcomePrice"), 0.0)
            shares = _to_float(item.get("shares") or item.get("size") or item.get("quantity"), 0.0)
            amount_usd = _to_float(item.get("amount") or item.get("amountUsd") or item.get("usdc"), round(price * shares, 4))
            market_slug = item.get("marketSlug") or item.get("slug") or item.get("market_slug") or item.get("conditionId") or "unknown-market"
            outcome = item.get("outcome") or item.get("token") or item.get("outcomeName") or "UNKNOWN"
            created_at = _iso(item.get("createdAt") or item.get("timestamp") or item.get("time"))
            trade_id = _trade_row_id(item, market_slug, outcome, side, created_at, index)
            trades.append(
                {
                    "source_trade_id": str(trade_id),
                    "source_handle": handle,
                    "source_wallet": wallet,
                    "market_slug": market_slug,
                    "market_title": item.get("title") or item.get("marketTitle") or market_slug,
                    "outcome": outcome,
                    "side": side,
                    "price": price,
                    "shares": shares if shares > 0 else round(amount_usd / price, 6) if price else 0.0,
                    "amount_usd": amount_usd,
                    "created_at": created_at,
                    "status": item.get("status") or "CONFIRMED",
                    "condition_id": _clean_id(item.get("conditionId") or item.get("condition_id") or item.get("marketId")),
                    "token_id": _clean_id(item.get("asset") or item.get("tokenId") or item.get("token_id") or item.get("outcomeTokenId")),
                }
            )
        trades.sort(key=lambda item: item["created_at"])
        return self._collapse_burst_trades(trades)

    def _collapse_burst_trades(self, trades: list[dict]) -> list[dict]:
        collapsed: list[dict] = []
        for trade in trades:
            if not collapsed:
                collapsed.append(trade)
                continue

            previous = collapsed[-1]
            if not self._can_merge_trades(previous, trade):
                collapsed.append(trade)
                continue

            previous_amount = _to_float(previous.get("amount_usd"), 0.0)
            trade_amount = _to_float(trade.get("amount_usd"), 0.0)
            merged_amount = round(previous_amount + trade_amount, 4)

            previous_shares = _to_float(previous.get("shares"), 0.0)
            trade_shares = _to_float(trade.get("shares"), 0.0)
            merged_shares = round(previous_shares + trade_shares, 6)

            weighted_notional = previous_amount + trade_amount
            if weighted_notional > 0:
                merged_price = round(
                    ((_to_float(previous.get("price"), 0.0) * previous_amount) + (_to_float(trade.get("price"), 0.0) * trade_amount))
                    / weighted_notional,
                    6,
                )
            else:
                merged_price = max(_to_float(previous.get("price"), 0.0), _to_float(trade.get("price"), 0.0))

            previous["amount_usd"] = merged_amount
            previous["shares"] = merged_shares
            previous["price"] = merged_price
            previous["created_at"] = max(previous.get("created_at", ""), trade.get("created_at", ""))
            previous["source_trade_id"] = f"{previous['source_trade_id']}+{trade['source_trade_id']}"
            previous["status"] = trade.get("status") or previous.get("status")
        return collapsed

    def _can_merge_trades(self, previous: dict, current: dict) -> bool:
        keys_to_match = ("source_wallet", "market_slug", "outcome", "side")
        if any((previous.get(key) or "") != (current.get(key) or "") for key in keys_to_match):
            return False

        previous_condition = _clean_id(previous.get("condition_id"))
        current_condition = _clean_id(current.get("condition_id"))
        if previous_condition and current_condition and previous_condition != current_condition:
            return False

        previous_token = _clean_id(previous.get("token_id"))
        current_token = _clean_id(current.get("token_id"))
        if previous_token and current_token and previous_token != current_token:
            return False

        previous_time = _parse_iso(previous.get("created_at", ""))
        current_time = _parse_iso(current.get("created_at", ""))
        return (current_time - previous_time).total_seconds() <= BURST_TRADE_WINDOW_SECONDS

    def _normalize_positions(self, payload: Any) -> list[dict]:
        items = payload if isinstance(payload, list) else payload.get("data") or payload.get("items") or payload.get("positions") or []
        positions = []
        for item in items:
            if not isinstance(item, dict):
                continue
            shares = _to_float(item.get("shares") or item.get("size") or item.get("quantity"), 0.0)
            if shares <= 0:
                continue
            notional_usd = _to_float(
                item.get("currentValue")
                or item.get("amount")
                or item.get("amountUsd")
                or item.get("value"),
                0.0,
            )
            live_price = _round_market_price(
                item.get("outcomePrice")
                or item.get("curPrice")
                or item.get("currentPrice")
                or item.get("markPrice")
                or item.get("price"),
                0.0,
            )
            derived_price = round(notional_usd / shares, 2) if notional_usd > 0 and shares > 0 else 0.0
            price = live_price or derived_price
            market_slug = item.get("marketSlug") or item.get("slug") or item.get("conditionId") or "unknown-market"
            outcome = item.get("outcome") or item.get("token") or item.get("outcomeName") or "UNKNOWN"
            side = "BUY"
            position_key = f"{market_slug}:{outcome}"
            positions.append(
                {
                    "position_key": position_key,
                    "market_slug": market_slug,
                    "market_title": item.get("title") or item.get("marketTitle") or market_slug,
                    "outcome": outcome,
                    "side": side,
                    "price": price,
                    "shares": shares,
                    "notional_usd": notional_usd if notional_usd > 0 else round(shares * price, 4),
                    "updated_at": _iso(item.get("updatedAt") or item.get("timestamp")),
                    "condition_id": _clean_id(item.get("conditionId") or item.get("condition_id") or item.get("marketId")),
                    "token_id": _clean_id(item.get("asset") or item.get("tokenId") or item.get("token_id") or item.get("outcomeTokenId")),
                }
            )
        return positions
