#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ModuleNotFoundError:
    hashes = None
    serialization = None
    padding = None


DEFAULT_BASE_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}

DEFAULT_CRYPTO_SERIES = [
    "KXBTC15M",
    "KXETH15M",
    "KXSOL15M",
    "KXXRP15M",
    "KXBNB15M",
    "KXDOGE15M",
    "KXHYPE15M",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dollars_to_cents(value: Any) -> Optional[int]:
    parsed = parse_float(value)
    if parsed is None:
        return None
    return int(round(parsed * 100))


def fp_to_contracts(value: Any) -> float:
    parsed = parse_float(value)
    return parsed if parsed is not None else 0.0


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class BotConfig:
    mode: str = os.getenv("KALSHI_MODE", "paper").strip().lower()
    environment: str = os.getenv("KALSHI_ENV", "prod").strip().lower()
    base_url: str = os.getenv("KALSHI_BASE_URL", "")
    api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    series_tickers: List[str] = field(
        default_factory=lambda: [
            item.strip()
            for item in os.getenv("KALSHI_SERIES", ",".join(DEFAULT_CRYPTO_SERIES)).split(",")
            if item.strip()
        ]
    )
    scan_interval_seconds: float = env_float("KALSHI_SCAN_INTERVAL_SECONDS", 5.0)
    min_minutes_to_close: float = env_float("KALSHI_MIN_MINUTES_TO_CLOSE", 1.0)
    max_minutes_to_close: float = env_float("KALSHI_MAX_MINUTES_TO_CLOSE", 5.0)
    target_minutes_to_close: float = env_float("KALSHI_TARGET_MINUTES_TO_CLOSE", 3.0)
    min_favorite_price_cents: int = env_int("KALSHI_MIN_FAVORITE_PRICE_CENTS", 88)
    max_entry_price_cents: int = env_int("KALSHI_MAX_ENTRY_PRICE_CENTS", 95)
    max_spread_cents: int = env_int("KALSHI_MAX_SPREAD_CENTS", 2)
    min_bid_support_contracts: int = env_int("KALSHI_MIN_BID_SUPPORT_CONTRACTS", 20)
    min_ask_liquidity_contracts: int = env_int("KALSHI_MIN_ASK_LIQUIDITY_CONTRACTS", 10)
    min_open_interest_contracts: int = env_int("KALSHI_MIN_OPEN_INTEREST_CONTRACTS", 50)
    min_volume_contracts: int = env_int("KALSHI_MIN_VOLUME_CONTRACTS", 100)
    contracts_per_trade: int = env_int("KALSHI_CONTRACTS_PER_TRADE", 10)
    max_concurrent_positions: int = env_int("KALSHI_MAX_CONCURRENT_POSITIONS", 2)
    max_trade_notional_dollars: float = env_float("KALSHI_MAX_TRADE_NOTIONAL_DOLLARS", 15.0)
    max_total_exposure_dollars: float = env_float("KALSHI_MAX_TOTAL_EXPOSURE_DOLLARS", 30.0)
    entry_cooldown_seconds: int = env_int("KALSHI_ENTRY_COOLDOWN_SECONDS", 30)
    fee_rate_on_winnings: float = env_float("KALSHI_FEE_RATE_ON_WINNINGS", 0.07)
    request_timeout_seconds: float = env_float("KALSHI_REQUEST_TIMEOUT_SECONDS", 10.0)
    log_level: str = os.getenv("KALSHI_LOG_LEVEL", "INFO").upper()

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return DEFAULT_BASE_URLS[self.environment]

    def validate(self) -> None:
        if self.mode not in {"paper", "live"}:
            raise ValueError("KALSHI_MODE must be 'paper' or 'live'.")
        if self.environment not in DEFAULT_BASE_URLS:
            raise ValueError("KALSHI_ENV must be 'demo' or 'prod'.")
        if self.min_minutes_to_close >= self.max_minutes_to_close:
            raise ValueError("KALSHI_MIN_MINUTES_TO_CLOSE must be less than KALSHI_MAX_MINUTES_TO_CLOSE.")
        if self.target_minutes_to_close < self.min_minutes_to_close:
            raise ValueError("KALSHI_TARGET_MINUTES_TO_CLOSE must be within the entry window.")
        if self.target_minutes_to_close > self.max_minutes_to_close:
            raise ValueError("KALSHI_TARGET_MINUTES_TO_CLOSE must be within the entry window.")
        if self.contracts_per_trade <= 0:
            raise ValueError("KALSHI_CONTRACTS_PER_TRADE must be positive.")
        if self.max_trade_notional_dollars <= 0 or self.max_total_exposure_dollars <= 0:
            raise ValueError("Risk limits must be positive.")
        if self.mode == "live" and (not self.api_key_id or not self.private_key_path):
            raise ValueError("Live mode requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")


@dataclass
class ContractQuote:
    side: str
    bid_cents: Optional[int]
    ask_cents: Optional[int]
    bid_size: float
    ask_size: float

    @property
    def spread_cents(self) -> Optional[int]:
        if self.bid_cents is None or self.ask_cents is None:
            return None
        return self.ask_cents - self.bid_cents


@dataclass
class MarketSnapshot:
    ticker: str
    title: str
    subtitle: str
    series_ticker: str
    status: str
    close_time: datetime
    volume_contracts: float
    open_interest_contracts: float
    yes: ContractQuote
    no: ContractQuote
    result: Optional[str]

    @property
    def seconds_to_close(self) -> float:
        return (self.close_time - utc_now()).total_seconds()

    def quote_for_side(self, side: str) -> ContractQuote:
        return self.yes if side == "yes" else self.no

    def favorite_side(self) -> Optional[str]:
        yes_reference = self.yes.bid_cents if self.yes.bid_cents is not None else self.yes.ask_cents
        no_reference = self.no.bid_cents if self.no.bid_cents is not None else self.no.ask_cents
        if yes_reference is None or no_reference is None:
            return None
        if yes_reference == no_reference:
            return None
        return "yes" if yes_reference > no_reference else "no"


@dataclass
class Decision:
    ticker: str
    side: str
    count: int
    price_cents: int
    score: float
    reason: str
    close_time: datetime
    title: str

    @property
    def notional_dollars(self) -> float:
        return self.count * self.price_cents / 100.0


@dataclass
class Position:
    ticker: str
    side: str
    count: int
    entry_price_cents: int
    entered_at: datetime
    close_time: datetime
    mode: str
    client_order_id: Optional[str] = None

    @property
    def notional_dollars(self) -> float:
        return self.count * self.entry_price_cents / 100.0


class KalshiClient:
    def __init__(self, config: BotConfig):
        self.config = config
        self.base_url = config.resolved_base_url()
        self.session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST", "DELETE"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"Accept": "application/json", "User-Agent": "kalshi-expiry-scalper/2.0"})
        self._private_key = None

    def _ensure_private_key(self) -> Any:
        if self._private_key is not None:
            return self._private_key
        if not self.config.private_key_path:
            raise RuntimeError("Private key path is not configured.")
        if serialization is None or hashes is None or padding is None:
            raise RuntimeError("cryptography is required for authenticated Kalshi requests.")
        with open(self.config.private_key_path, "rb") as handle:
            self._private_key = serialization.load_pem_private_key(handle.read(), password=None)
        return self._private_key

    def _signed_headers(self, method: str, path: str) -> Dict[str, str]:
        private_key = self._ensure_private_key()
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path.split('?')[0]}".encode("utf-8")
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.config.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        auth: bool = False,
    ) -> Dict[str, Any]:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        headers: Dict[str, str] = {}
        if auth:
            headers.update(self._signed_headers(method, f"{path}{query}"))

        response = self.session.request(
            method=method.upper(),
            url=f"{self.base_url}{path}",
            params=params,
            json=json_body,
            headers=headers,
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected payload from {path}")
        return payload

    def list_markets(
        self,
        *,
        series_ticker: str,
        status: str,
        min_close_ts: int,
        max_close_ts: int,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        cursor = None
        rows: List[Dict[str, Any]] = []
        while True:
            params: Dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": status,
                "min_close_ts": min_close_ts,
                "max_close_ts": max_close_ts,
                "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor
            payload = self.request("GET", "/markets", params=params)
            page_rows = payload.get("markets") or []
            if not isinstance(page_rows, list) or not page_rows:
                break
            rows.extend(page_rows)
            cursor = payload.get("cursor")
            if not cursor:
                break
        return rows

    def get_market(self, ticker: str) -> Dict[str, Any]:
        payload = self.request("GET", f"/markets/{ticker}")
        return payload.get("market") or {}

    def get_orderbook(self, ticker: str, depth: int = 10) -> Dict[str, Any]:
        payload = self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return payload.get("orderbook") or payload

    def create_order(self, decision: Decision) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "ticker": decision.ticker,
            "client_order_id": f"scalp-{uuid.uuid4().hex[:20]}",
            "action": "buy",
            "count": decision.count,
            "side": decision.side,
            "type": "limit",
            "time_in_force": "immediate_or_cancel",
        }
        if decision.side == "yes":
            body["yes_price"] = decision.price_cents
        else:
            body["no_price"] = decision.price_cents
        return self.request("POST", "/portfolio/orders", json_body=body, auth=True)


def best_level(levels: Sequence[Any]) -> Tuple[Optional[int], float]:
    if not isinstance(levels, Sequence):
        return None, 0.0
    best_price: Optional[int] = None
    best_qty = 0.0
    for level in levels:
        if not isinstance(level, Sequence) or len(level) < 2:
            continue
        price_cents = dollars_to_cents(level[0])
        qty = fp_to_contracts(level[1])
        if price_cents is None:
            continue
        if best_price is None or price_cents > best_price:
            best_price = price_cents
            best_qty = qty
    return best_price, best_qty


def build_market_snapshot(market: Dict[str, Any], orderbook: Dict[str, Any]) -> Optional[MarketSnapshot]:
    ticker = str(market.get("ticker") or "")
    if not ticker:
        return None
    close_time_raw = market.get("close_time")
    if not close_time_raw:
        return None

    yes_bid, yes_bid_size = best_level(orderbook.get("yes") or [])
    no_bid, no_bid_size = best_level(orderbook.get("no") or [])

    yes_ask = 100 - no_bid if no_bid is not None else None
    no_ask = 100 - yes_bid if yes_bid is not None else None
    yes_ask_size = no_bid_size if no_bid is not None else 0.0
    no_ask_size = yes_bid_size if yes_bid is not None else 0.0

    return MarketSnapshot(
        ticker=ticker,
        title=str(market.get("title") or ""),
        subtitle=str(market.get("subtitle") or ""),
        series_ticker=str(market.get("series_ticker") or ""),
        status=str(market.get("status") or "").lower(),
        close_time=parse_iso8601(str(close_time_raw)),
        volume_contracts=fp_to_contracts(market.get("volume") or market.get("volume_fp")),
        open_interest_contracts=fp_to_contracts(market.get("open_interest") or market.get("open_interest_fp")),
        yes=ContractQuote("yes", yes_bid, yes_ask, yes_bid_size, yes_ask_size),
        no=ContractQuote("no", no_bid, no_ask, no_bid_size, no_ask_size),
        result=str(market.get("result") or "").lower() or None,
    )


class DecisionEngine:
    def __init__(self, config: BotConfig):
        self.config = config

    def decide(self, snapshot: MarketSnapshot) -> Optional[Decision]:
        favorite_side = snapshot.favorite_side()
        if favorite_side is None:
            return None

        quote = snapshot.quote_for_side(favorite_side)
        if quote.ask_cents is None or quote.bid_cents is None:
            return None
        if snapshot.status != "open":
            return None

        seconds_to_close = snapshot.seconds_to_close
        min_seconds = self.config.min_minutes_to_close * 60
        max_seconds = self.config.max_minutes_to_close * 60
        if seconds_to_close < min_seconds or seconds_to_close > max_seconds:
            return None

        if quote.ask_cents < self.config.min_favorite_price_cents:
            return None
        if quote.ask_cents > self.config.max_entry_price_cents:
            return None

        spread = quote.spread_cents
        if spread is None or spread > self.config.max_spread_cents:
            return None
        if quote.bid_size < self.config.min_bid_support_contracts:
            return None
        if quote.ask_size < max(self.config.contracts_per_trade, self.config.min_ask_liquidity_contracts):
            return None
        if snapshot.open_interest_contracts < self.config.min_open_interest_contracts:
            return None
        if snapshot.volume_contracts < self.config.min_volume_contracts:
            return None

        count = self.config.contracts_per_trade
        notional_dollars = count * quote.ask_cents / 100.0
        if notional_dollars > self.config.max_trade_notional_dollars:
            return None

        target_seconds = self.config.target_minutes_to_close * 60
        time_penalty = abs(seconds_to_close - target_seconds) / max(1.0, max_seconds - min_seconds)
        score = (
            quote.bid_cents
            + (100 - quote.ask_cents) * 0.2
            + min(quote.bid_size, 100) * 0.08
            + min(quote.ask_size, 100) * 0.04
            + min(snapshot.open_interest_contracts, 500) * 0.01
            - spread * 4.0
            - time_penalty * 15.0
        )
        reason = (
            f"{favorite_side.upper()} ask {quote.ask_cents}c, bid {quote.bid_cents}c, spread {spread}c, "
            f"bid support {quote.bid_size:.0f}, ask liquidity {quote.ask_size:.0f}, "
            f"oi {snapshot.open_interest_contracts:.0f}, vol {snapshot.volume_contracts:.0f}, "
            f"ttc {int(seconds_to_close)}s"
        )
        return Decision(
            ticker=snapshot.ticker,
            side=favorite_side,
            count=count,
            price_cents=quote.ask_cents,
            score=score,
            reason=reason,
            close_time=snapshot.close_time,
            title=snapshot.title or snapshot.subtitle or snapshot.ticker,
        )


class PaperBroker:
    def __init__(self, config: BotConfig):
        self.config = config
        self.positions: Dict[str, Position] = {}
        self.realized_pnl_dollars = 0.0
        self.last_entry_ts: Dict[str, float] = {}

    def current_exposure_dollars(self) -> float:
        return sum(position.notional_dollars for position in self.positions.values())

    def can_enter(self, decision: Decision) -> bool:
        if decision.ticker in self.positions:
            return False
        if len(self.positions) >= self.config.max_concurrent_positions:
            return False
        if self.current_exposure_dollars() + decision.notional_dollars > self.config.max_total_exposure_dollars:
            return False
        last_ts = self.last_entry_ts.get(decision.ticker)
        if last_ts and time.time() - last_ts < self.config.entry_cooldown_seconds:
            return False
        return True

    def enter(self, decision: Decision) -> Position:
        position = Position(
            ticker=decision.ticker,
            side=decision.side,
            count=decision.count,
            entry_price_cents=decision.price_cents,
            entered_at=utc_now(),
            close_time=decision.close_time,
            mode="paper",
        )
        self.positions[position.ticker] = position
        self.last_entry_ts[position.ticker] = time.time()
        return position

    def settle(self, snapshot: MarketSnapshot) -> Optional[float]:
        position = self.positions.get(snapshot.ticker)
        if position is None or snapshot.result not in {"yes", "no"}:
            return None
        won = snapshot.result == position.side
        entry = position.entry_price_cents / 100.0
        gross_pnl = (1.0 - entry) * position.count if won else -entry * position.count
        if won:
            gross_pnl *= 1.0 - self.config.fee_rate_on_winnings
        self.realized_pnl_dollars += gross_pnl
        del self.positions[snapshot.ticker]
        return gross_pnl


class LiveBroker(PaperBroker):
    def __init__(self, config: BotConfig, client: KalshiClient):
        super().__init__(config)
        self.client = client

    def enter(self, decision: Decision) -> Position:
        response = self.client.create_order(decision)
        order = response.get("order") or response
        position = Position(
            ticker=decision.ticker,
            side=decision.side,
            count=int(order.get("remaining_count", 0) or decision.count),
            entry_price_cents=decision.price_cents,
            entered_at=utc_now(),
            close_time=decision.close_time,
            mode="live",
            client_order_id=str(order.get("client_order_id") or ""),
        )
        self.positions[position.ticker] = position
        self.last_entry_ts[position.ticker] = time.time()
        return position


def collect_snapshots(client: KalshiClient, config: BotConfig) -> List[MarketSnapshot]:
    now_ts = int(time.time())
    min_close_ts = now_ts + int(config.min_minutes_to_close * 60)
    max_close_ts = now_ts + int(config.max_minutes_to_close * 60)
    snapshots: List[MarketSnapshot] = []

    for series_ticker in config.series_tickers:
        markets = client.list_markets(
            series_ticker=series_ticker,
            status="open",
            min_close_ts=min_close_ts,
            max_close_ts=max_close_ts,
        )
        for market in markets:
            ticker = market.get("ticker")
            if not ticker:
                continue
            try:
                orderbook = client.get_orderbook(str(ticker))
                snapshot = build_market_snapshot(market, orderbook)
            except Exception as exc:
                logging.warning("Skipping %s after market data error: %s", ticker, exc)
                continue
            if snapshot is not None:
                snapshots.append(snapshot)
    snapshots.sort(key=lambda item: item.close_time)
    return snapshots


def refresh_positions(client: KalshiClient, broker: PaperBroker) -> None:
    if not broker.positions:
        return
    for ticker in list(broker.positions):
        try:
            market = client.get_market(ticker)
            orderbook = client.get_orderbook(ticker)
            snapshot = build_market_snapshot(market, orderbook)
        except Exception as exc:
            logging.warning("Could not refresh %s for settlement check: %s", ticker, exc)
            continue
        if snapshot is None:
            continue
        pnl = broker.settle(snapshot)
        if pnl is not None:
            logging.info("Settled %s %s for %.2f dollars", snapshot.ticker, broker.config.mode, pnl)


def run_cycle(client: KalshiClient, engine: DecisionEngine, broker: PaperBroker) -> List[Decision]:
    refresh_positions(client, broker)
    decisions: List[Decision] = []
    for snapshot in collect_snapshots(client, broker.config):
        decision = engine.decide(snapshot)
        if decision is None:
            continue
        decisions.append(decision)

    decisions.sort(key=lambda item: item.score, reverse=True)

    for decision in decisions:
        if not broker.can_enter(decision):
            continue
        position = broker.enter(decision)
        logging.info(
            "Entered %s %s x%d at %dc on %s | %s",
            position.mode,
            decision.side.upper(),
            decision.count,
            decision.price_cents,
            decision.ticker,
            decision.reason,
        )
    return decisions


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi late-expiry crypto scalper")
    parser.add_argument("--once", action="store_true", help="Run one decision cycle and exit")
    parser.add_argument("--mode", choices=["paper", "live"], help="Override KALSHI_MODE")
    parser.add_argument("--env", choices=["demo", "prod"], help="Override KALSHI_ENV")
    parser.add_argument("--log-level", help="Override KALSHI_LOG_LEVEL")
    return parser


def apply_cli_overrides(config: BotConfig, args: argparse.Namespace) -> BotConfig:
    if args.mode:
        config.mode = args.mode
    if args.env:
        config.environment = args.env
    if args.log_level:
        config.log_level = args.log_level.upper()
    return config


def main() -> None:
    args = build_arg_parser().parse_args()
    config = apply_cli_overrides(BotConfig(), args)
    config.validate()
    configure_logging(config.log_level)

    client = KalshiClient(config)
    engine = DecisionEngine(config)
    broker: PaperBroker
    if config.mode == "live":
        broker = LiveBroker(config, client)
    else:
        broker = PaperBroker(config)

    logging.info(
        "Starting bot in %s mode against %s for %s",
        config.mode,
        config.resolved_base_url(),
        ",".join(config.series_tickers),
    )

    while True:
        decisions = run_cycle(client, engine, broker)
        if decisions:
            best = decisions[0]
            logging.info("Top candidate: %s %s @ %dc score %.2f", best.ticker, best.side.upper(), best.price_cents, best.score)
        else:
            logging.info("No candidates passed filters in this cycle.")
        logging.info(
            "Open positions: %d | exposure: %.2f | realized pnl: %.2f",
            len(broker.positions),
            broker.current_exposure_dollars(),
            broker.realized_pnl_dollars,
        )
        if args.once:
            break
        time.sleep(config.scan_interval_seconds)


if __name__ == "__main__":
    main()
