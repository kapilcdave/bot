#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from hedge_engine import HedgeBook, HedgeConfig, HedgeEngine, HedgeInputs, HedgeState


load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


MODE = os.getenv("KALSHI_MODE", "paper").strip().lower()
ENV = os.getenv("KALSHI_ENV", "prod").strip().lower()
SERIES_TICKER = os.getenv("KALSHI_SERIES", "KXBTC15M").strip()
SCAN_INTERVAL_SECONDS = float(os.getenv("KALSHI_SCAN_INTERVAL_SECONDS", "5"))
MIN_MINUTES_TO_CLOSE = float(os.getenv("KALSHI_MIN_MINUTES_TO_CLOSE", "1"))
MAX_MINUTES_TO_CLOSE = float(os.getenv("KALSHI_MAX_MINUTES_TO_CLOSE", "15"))
BINANCE_URL = os.getenv("BINANCE_BTC_URL", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
BTC_ANNUAL_VOL = float(os.getenv("BTC_ANNUAL_VOL", "0.65"))
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", os.getenv("KALSHI_KEY_ID", ""))
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "private_key.pem")

BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if ENV == "demo"
    else "https://api.elections.kalshi.com/trade-api/v2"
)

BASE_ORDER_SIZE = float(os.getenv("KALSHI_BASE_ORDER_SIZE", "2"))
MAX_SIDE_SHARES = float(os.getenv("KALSHI_MAX_SIDE_SHARES", "25"))
MAX_TOTAL_COST = float(os.getenv("KALSHI_MAX_TOTAL_COST", "60"))
FAIR_VALUE_EDGE_CENTS = float(os.getenv("KALSHI_FAIR_VALUE_EDGE_CENTS", "4"))
PRESS_EDGE_CENTS = float(os.getenv("KALSHI_PRESS_EDGE_CENTS", "6"))
CHEAP_HEDGE_PRICE_CAP = float(os.getenv("KALSHI_CHEAP_HEDGE_PRICE_CAP", "0.25"))
INVENTORY_IMBALANCE_TRIGGER = float(os.getenv("KALSHI_INVENTORY_IMBALANCE_TRIGGER", "2"))
PAPER_FILL_OFFSET_CENTS = float(os.getenv("KALSHI_PAPER_FILL_OFFSET_CENTS", "0"))
ALLOW_LIVE_TRADING = env_bool("KALSHI_ALLOW_LIVE_TRADING", False)
STATE_PATH = os.getenv("KALSHI_STATE_PATH", "kalshi_bot_state.json")


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _load_private_key():
    with open(KALSHI_PRIVATE_KEY_PATH, "rb") as handle:
        return serialization.load_pem_private_key(handle.read(), password=None)


def _sign(private_key, method: str, path: str) -> dict[str, str]:
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path.split("?")[0]
    sig = private_key.sign(
        msg.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


def request_json(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
    private_key=None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "kalshi-15m-hedge-bot/1.0"}
    if private_key is not None:
        headers.update(_sign(private_key, method, path))
    response = requests.request(
        method=method,
        url=BASE_URL + path,
        params=params,
        json=payload,
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"Unexpected response type for {path}")
    return body


def get_btc_price() -> Optional[float]:
    try:
        response = requests.get(BINANCE_URL, timeout=3)
        response.raise_for_status()
        payload = response.json()
        return float(payload["price"])
    except Exception as exc:
        log.error("BTC price fetch failed: %s", exc)
        return None


def get_active_market(private_key) -> Optional[dict[str, Any]]:
    try:
        payload = request_json(
            "GET",
            "/markets",
            params={"series_ticker": SERIES_TICKER, "status": "open", "limit": 20},
            private_key=private_key,
        )
        markets = payload.get("markets") or []
        now = datetime.now(timezone.utc)
        candidates: list[dict[str, Any]] = []
        for market in markets:
            close_time_raw = market.get("close_time")
            open_time_raw = market.get("open_time")
            if not close_time_raw or not open_time_raw:
                continue
            close_time = datetime.fromisoformat(str(close_time_raw).replace("Z", "+00:00"))
            open_time = datetime.fromisoformat(str(open_time_raw).replace("Z", "+00:00"))
            minutes_to_close = (close_time - now).total_seconds() / 60.0
            if minutes_to_close < MIN_MINUTES_TO_CLOSE or minutes_to_close > MAX_MINUTES_TO_CLOSE:
                continue
            market = dict(market)
            market["_close_time"] = close_time
            market["_open_time"] = open_time
            market["_minutes_to_close"] = minutes_to_close
            market["_elapsed_seconds"] = max(0.0, (now - open_time).total_seconds())
            market["_total_seconds"] = max(1.0, (close_time - open_time).total_seconds())
            candidates.append(market)
        if not candidates:
            return None
        candidates.sort(key=lambda item: item["_minutes_to_close"])
        return candidates[0]
    except Exception as exc:
        log.error("Market fetch failed: %s", exc)
        return None


def get_orderbook(private_key, ticker: str) -> Optional[dict[str, Any]]:
    try:
        payload = request_json("GET", f"/markets/{ticker}/orderbook", private_key=private_key)
        return payload.get("orderbook")
    except Exception as exc:
        log.error("Orderbook fetch failed: %s", exc)
        return None


def parse_top_levels(orderbook: dict[str, Any]) -> dict[str, Optional[float]]:
    try:
        yes_bids = orderbook.get("yes") or []
        no_bids = orderbook.get("no") or []
        yes_bid = float(yes_bids[0]["price"]) if yes_bids else None
        no_bid = float(no_bids[0]["price"]) if no_bids else None
        yes_bid_size = float(yes_bids[0].get("quantity", yes_bids[0].get("count", 0.0))) if yes_bids else 0.0
        no_bid_size = float(no_bids[0].get("quantity", no_bids[0].get("count", 0.0))) if no_bids else 0.0
        yes_ask = None if no_bid is None else 1.0 - no_bid
        no_ask = None if yes_bid is None else 1.0 - yes_bid
        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "yes_bid_size": yes_bid_size,
            "no_bid_size": no_bid_size,
        }
    except Exception as exc:
        log.error("Orderbook parse failed: %s", exc)
        return {
            "yes_bid": None,
            "yes_ask": None,
            "no_bid": None,
            "no_ask": None,
            "yes_bid_size": 0.0,
            "no_bid_size": 0.0,
        }


def parse_strike_from_ticker(ticker: str) -> Optional[float]:
    try:
        for part in ticker.split("-"):
            if part.startswith("B") and part[1:].isdigit():
                return float(part[1:])
    except Exception:
        return None
    return None


def _norm_cdf(x: float) -> float:
    a = [0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429]
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x) / math.sqrt(2)
    t = 1 / (1 + p * x)
    y = 1 - (((((a[4] * t + a[3]) * t + a[2]) * t + a[1]) * t + a[0]) * t) * math.exp(-x * x)
    return 0.5 * (1 + sign * y)


def gbm_yes_fair_value(spot: float, strike: float, minutes_left: float, annual_vol: float) -> float:
    if minutes_left <= 0:
        return 1.0 if spot > strike else 0.0
    t = minutes_left / (365 * 24 * 60)
    if t <= 0 or annual_vol <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) - 0.5 * annual_vol * annual_vol * t) / (annual_vol * math.sqrt(t))
    return _norm_cdf(d2)


@dataclass
class SideInventory:
    shares: float = 0.0
    total_cost: float = 0.0

    @property
    def avg_price(self) -> float:
        return 0.0 if self.shares <= 0 else self.total_cost / self.shares

    def buy(self, shares: float, price: float) -> None:
        self.shares += shares
        self.total_cost += shares * price

    def to_dict(self) -> dict[str, float]:
        return {"shares": self.shares, "total_cost": self.total_cost}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SideInventory":
        return cls(
            shares=float(payload.get("shares", 0.0) or 0.0),
            total_cost=float(payload.get("total_cost", 0.0) or 0.0),
        )


@dataclass
class BookState:
    ticker: str = ""
    market_open_ts: float = 0.0
    yes: SideInventory = field(default_factory=SideInventory)
    no: SideInventory = field(default_factory=SideInventory)
    trade_log: list[dict[str, Any]] = field(default_factory=list)
    session_pnl_locked: float = 0.0

    def reset_for_market(self, ticker: str, open_ts: float) -> None:
        if self.ticker and self.ticker != ticker:
            self.yes = SideInventory()
            self.no = SideInventory()
            self.trade_log = []
            self.session_pnl_locked = 0.0
        self.ticker = ticker
        self.market_open_ts = open_ts

    @property
    def total_cost(self) -> float:
        return self.yes.total_cost + self.no.total_cost

    @property
    def total_shares(self) -> float:
        return self.yes.shares + self.no.shares

    @property
    def pnl_if_yes(self) -> float:
        return self.yes.shares - self.total_cost

    @property
    def pnl_if_no(self) -> float:
        return self.no.shares - self.total_cost

    @property
    def pair_shares(self) -> float:
        return min(self.yes.shares, self.no.shares)

    @property
    def hard_locked(self) -> bool:
        return self.pnl_if_yes >= 0 and self.pnl_if_no >= 0

    def apply_fill(self, side: str, shares: float, price: float, reason: str, timestamp: float) -> None:
        if side == "yes":
            self.yes.buy(shares, price)
        else:
            self.no.buy(shares, price)
        self.trade_log.append(
            {
                "ts": timestamp,
                "ticker": self.ticker,
                "side": side,
                "shares": shares,
                "price": price,
                "reason": reason,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "market_open_ts": self.market_open_ts,
            "yes": self.yes.to_dict(),
            "no": self.no.to_dict(),
            "trade_log": self.trade_log,
            "session_pnl_locked": self.session_pnl_locked,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BookState":
        return cls(
            ticker=str(payload.get("ticker", "") or ""),
            market_open_ts=float(payload.get("market_open_ts", 0.0) or 0.0),
            yes=SideInventory.from_dict(payload.get("yes", {})),
            no=SideInventory.from_dict(payload.get("no", {})),
            trade_log=list(payload.get("trade_log", []) or []),
            session_pnl_locked=float(payload.get("session_pnl_locked", 0.0) or 0.0),
        )


@dataclass(frozen=True)
class MarketSnapshot:
    ticker: str
    minutes_to_close: float
    elapsed_seconds: float
    total_seconds: float
    spot_price: float
    strike_price: float
    fair_yes: float
    fair_no: float
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_bid_size: float
    no_bid_size: float

    @property
    def pair_ask_cost(self) -> float:
        return self.yes_ask + self.no_ask


def get_live_positions(private_key) -> list[dict[str, Any]]:
    payload = request_json("GET", "/portfolio/positions", private_key=private_key)
    positions = payload.get("market_positions") or payload.get("positions") or []
    return positions if isinstance(positions, list) else []


def get_live_orders(private_key, ticker: str) -> list[dict[str, Any]]:
    payload = request_json(
        "GET",
        "/portfolio/orders",
        params={"ticker": ticker, "status": "open", "limit": 200},
        private_key=private_key,
    )
    orders = payload.get("orders") or []
    return orders if isinstance(orders, list) else []


def _extract_side(payload: dict[str, Any]) -> Optional[str]:
    side = payload.get("side")
    if isinstance(side, str) and side.lower() in {"yes", "no"}:
        return side.lower()
    position = payload.get("position")
    if isinstance(position, str) and position.lower() in {"yes", "no"}:
        return position.lower()
    return None


def _extract_float(payload: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def rebuild_inventory_from_positions(positions: list[dict[str, Any]], ticker: str) -> tuple[SideInventory, SideInventory]:
    yes = SideInventory()
    no = SideInventory()
    for position in positions:
        if str(position.get("ticker", "")) != ticker:
            continue
        side = _extract_side(position)
        if side not in {"yes", "no"}:
            continue
        shares = _extract_float(position, "quantity", "count", "position_count", "contracts")
        avg_price = _extract_float(position, "average_price", "avg_price", "cost_basis", "price")
        if shares <= 0:
            continue
        inventory = yes if side == "yes" else no
        inventory.shares = shares
        inventory.total_cost = shares * avg_price
    return yes, no


def summarize_open_orders(orders: list[dict[str, Any]]) -> dict[str, float]:
    summary = {"yes": 0.0, "no": 0.0}
    for order in orders:
        side = _extract_side(order)
        if side not in summary:
            continue
        summary[side] += _extract_float(order, "remaining_count", "quantity", "count")
    return summary


def paper_fill_price(side: str, snapshot: MarketSnapshot) -> float:
    raw = snapshot.yes_ask if side == "yes" else snapshot.no_ask
    adjusted = raw + (PAPER_FILL_OFFSET_CENTS / 100.0)
    return clamp(adjusted, 0.01, 0.99)


def should_accumulate(side: str, state: BookState, snapshot: MarketSnapshot) -> tuple[bool, str]:
    inventory = state.yes if side == "yes" else state.no
    fair = snapshot.fair_yes if side == "yes" else snapshot.fair_no
    ask = snapshot.yes_ask if side == "yes" else snapshot.no_ask
    opposite_shares = state.no.shares if side == "yes" else state.yes.shares
    edge_cents = (fair - ask) * 100.0

    if inventory.shares + BASE_ORDER_SIZE > MAX_SIDE_SHARES:
        return False, "side_cap"
    if state.total_cost + (ask * BASE_ORDER_SIZE) > MAX_TOTAL_COST:
        return False, "budget_cap"
    if state.hard_locked:
        return False, "hard_lock"
    if edge_cents >= PRESS_EDGE_CENTS:
        return True, "press_fair_value"
    if inventory.shares < opposite_shares and edge_cents >= FAIR_VALUE_EDGE_CENTS:
        return True, "rebalance_to_pair"
    if ask <= CHEAP_HEDGE_PRICE_CAP and abs(state.yes.shares - state.no.shares) < INVENTORY_IMBALANCE_TRIGGER:
        return True, "cheap_pair_inventory"
    return False, "no_edge"


def place_live_buy_order(private_key, ticker: str, side: str, shares: float, price: float) -> dict[str, Any]:
    count = max(1, int(round(shares)))
    path = "/portfolio/orders"
    payload = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count": count,
        "time_in_force": "ioc",
    }
    price_key = "yes_price" if side == "yes" else "no_price"
    payload[price_key] = f"{price:.4f}"
    return request_json("POST", path, payload=payload, private_key=private_key)


class DualSideKalshiBot:
    def __init__(self):
        self.private_key = None
        if MODE == "live":
            if not ALLOW_LIVE_TRADING:
                raise RuntimeError("Set KALSHI_ALLOW_LIVE_TRADING=true before using live mode.")
            if not KALSHI_API_KEY_ID:
                raise RuntimeError("KALSHI_API_KEY_ID is required for live mode.")
            self.private_key = _load_private_key()
        self.hedge_engine = HedgeEngine(HedgeConfig())
        self.hedge_state = HedgeState()
        self.book_state = BookState()
        self.open_orders: list[dict[str, Any]] = []
        self.load_state()

    def load_state(self) -> None:
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return
            self.book_state = BookState.from_dict(payload.get("book_state", {}))
            hedge_payload = payload.get("hedge_state", {})
            self.hedge_state = HedgeState(
                hedges_fired=int(hedge_payload.get("hedges_fired", 0) or 0),
                budget_spent=float(hedge_payload.get("budget_spent", 0.0) or 0.0),
                last_hedge_ts=float(hedge_payload.get("last_hedge_ts", -1e18) or -1e18),
            )
            open_orders = payload.get("open_orders", [])
            self.open_orders = open_orders if isinstance(open_orders, list) else []
            log.info("Loaded persisted state from %s", STATE_PATH)
        except Exception as exc:
            log.warning("State load failed: %s", exc)

    def save_state(self) -> None:
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "book_state": self.book_state.to_dict(),
            "hedge_state": {
                "hedges_fired": self.hedge_state.hedges_fired,
                "budget_spent": self.hedge_state.budget_spent,
                "last_hedge_ts": self.hedge_state.last_hedge_ts,
            },
            "open_orders": self.open_orders,
        }
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def build_snapshot(self, market: dict[str, Any], orderbook: dict[str, Any], spot_price: float) -> Optional[MarketSnapshot]:
        top = parse_top_levels(orderbook)
        yes_bid = top["yes_bid"]
        yes_ask = top["yes_ask"]
        no_bid = top["no_bid"]
        no_ask = top["no_ask"]
        if None in {yes_bid, yes_ask, no_bid, no_ask}:
            return None
        strike = parse_strike_from_ticker(str(market["ticker"]))
        if strike is None:
            return None
        minutes_to_close = float(market["_minutes_to_close"])
        fair_yes = clamp(gbm_yes_fair_value(spot_price, strike, minutes_to_close, BTC_ANNUAL_VOL), 0.01, 0.99)
        return MarketSnapshot(
            ticker=str(market["ticker"]),
            minutes_to_close=minutes_to_close,
            elapsed_seconds=float(market["_elapsed_seconds"]),
            total_seconds=float(market["_total_seconds"]),
            spot_price=spot_price,
            strike_price=strike,
            fair_yes=fair_yes,
            fair_no=1.0 - fair_yes,
            yes_bid=float(yes_bid),
            yes_ask=float(yes_ask),
            no_bid=float(no_bid),
            no_ask=float(no_ask),
            yes_bid_size=float(top["yes_bid_size"]),
            no_bid_size=float(top["no_bid_size"]),
        )

    def current_hedge_inputs(self, snapshot: MarketSnapshot) -> HedgeInputs:
        imbalance = abs(self.book_state.yes.shares - self.book_state.no.shares)
        leader_now = "yes" if snapshot.fair_yes >= 0.5 else "no"
        leader_book = "yes" if self.book_state.yes.shares >= self.book_state.no.shares else "no"
        cvd_flips = 1 if leader_now != leader_book else 0
        momentum_flips = 1 if (snapshot.fair_yes > snapshot.yes_ask and leader_book == "no") or (snapshot.fair_no > snapshot.no_ask and leader_book == "yes") else 0
        obi_flips = 1 if (snapshot.yes_bid_size > snapshot.no_bid_size and leader_book == "no") or (snapshot.no_bid_size > snapshot.yes_bid_size and leader_book == "yes") else 0
        return HedgeInputs(
            current_yes_price=snapshot.yes_ask,
            current_no_price=snapshot.no_ask,
            elapsed_seconds=snapshot.elapsed_seconds,
            total_seconds=snapshot.total_seconds,
            volatility=BTC_ANNUAL_VOL,
            cumulative_volume_delta_flips=cvd_flips + int(imbalance >= INVENTORY_IMBALANCE_TRIGGER),
            short_term_momentum_flips=momentum_flips,
            orderbook_imbalance_flips=obi_flips,
            pair_cost=snapshot.pair_ask_cost,
            mid_price=(snapshot.yes_bid + snapshot.yes_ask) / 2.0,
            flow_decay=1.0,
        )

    def reconcile_live_state(self) -> None:
        if MODE != "live":
            return
        try:
            positions = get_live_positions(self.private_key)
            yes, no = rebuild_inventory_from_positions(positions, self.book_state.ticker)
            self.book_state.yes = yes
            self.book_state.no = no
        except Exception as exc:
            log.warning("Position reconciliation failed: %s", exc)
        try:
            self.open_orders = get_live_orders(self.private_key, self.book_state.ticker)
        except Exception as exc:
            log.warning("Open-order fetch failed: %s", exc)

    def execute_buy(self, side: str, shares: float, price: float, reason: str) -> bool:
        now_ts = time.time()
        if MODE == "paper":
            self.book_state.apply_fill(side, shares, price, reason, now_ts)
            self.open_orders = []
            self.save_state()
            log.info("PAPER FILL %s %.1f @ %.2f | %s", side.upper(), shares, price, reason)
            return True

        response = place_live_buy_order(self.private_key, self.book_state.ticker, side, shares, price)
        order = response.get("order", response)
        if isinstance(order, dict):
            self.open_orders.append(order)
        self.reconcile_live_state()
        self.book_state.trade_log.append(
            {
                "ts": now_ts,
                "ticker": self.book_state.ticker,
                "side": side,
                "shares_requested": shares,
                "price_requested": price,
                "reason": reason,
                "live_response": response,
            }
        )
        self.save_state()
        log.info("LIVE ORDER %s %.1f @ %.2f | %s | %s", side.upper(), shares, price, reason, json.dumps(response))
        return True

    def maybe_accumulate(self, snapshot: MarketSnapshot) -> None:
        for side in ("yes", "no"):
            should_buy, reason = should_accumulate(side, self.book_state, snapshot)
            if not should_buy:
                continue
            price = paper_fill_price(side, snapshot)
            self.execute_buy(side, BASE_ORDER_SIZE, price, reason)

    def maybe_hedge(self, snapshot: MarketSnapshot) -> None:
        hedge_book = HedgeBook(
            yes_shares=self.book_state.yes.shares,
            yes_avg_price=self.book_state.yes.avg_price,
            no_shares=self.book_state.no.shares,
            no_avg_price=self.book_state.no.avg_price,
        )
        decision = self.hedge_engine.decide(
            hedge_book,
            self.current_hedge_inputs(snapshot),
            self.hedge_state,
            time.time(),
        )
        if not decision.fire:
            log.info(
                "HEDGE HOLD urgency=%.2f blocked=%s components=%s",
                decision.urgency,
                decision.blocked_reason,
                {k: round(v, 2) for k, v in decision.components.items()},
            )
            return
        self.execute_buy(decision.hedge_side, decision.hedge_shares, decision.hedge_price, "hedge_engine")
        self.hedge_engine.apply_decision(self.hedge_state, decision, time.time())
        log.info(
            "HEDGE FIRE %s %.1f @ %.2f urgency=%.2f cost=%s",
            decision.hedge_side.upper(),
            decision.hedge_shares,
            decision.hedge_price,
            decision.urgency,
            fmt_money(decision.hedge_cost),
        )

    def log_console(self, snapshot: MarketSnapshot) -> None:
        open_order_summary = summarize_open_orders(self.open_orders)
        lines = [
            "=" * 72,
            f"Market {snapshot.ticker} | mode={MODE} env={ENV} series={SERIES_TICKER}",
            f"Spot={snapshot.spot_price:,.2f} strike={snapshot.strike_price:,.0f} ttc={snapshot.minutes_to_close:.2f}m",
            (
                f"YES bid/ask={snapshot.yes_bid:.2f}/{snapshot.yes_ask:.2f} fair={snapshot.fair_yes:.2f} "
                f"| NO bid/ask={snapshot.no_bid:.2f}/{snapshot.no_ask:.2f} fair={snapshot.fair_no:.2f}"
            ),
            (
                f"Inventory YES={self.book_state.yes.shares:.1f}@{self.book_state.yes.avg_price:.2f} "
                f"NO={self.book_state.no.shares:.1f}@{self.book_state.no.avg_price:.2f} "
                f"cost={fmt_money(self.book_state.total_cost)}"
            ),
            (
                f"Terminal PnL YES={self.book_state.pnl_if_yes:+.2f} "
                f"NO={self.book_state.pnl_if_no:+.2f} "
                f"hard_lock={self.book_state.hard_locked}"
            ),
            (
                f"Hedge budget={fmt_money(self.hedge_engine.config.hedge_budget_dollars - self.hedge_state.budget_spent)} "
                f"hedges_fired={self.hedge_state.hedges_fired} open_orders={len(self.open_orders)}"
            ),
            (
                f"Open Order Shares YES={open_order_summary['yes']:.1f} "
                f"NO={open_order_summary['no']:.1f}"
            ),
        "=" * 72,
        ]
        if MODE == "live":
            lines.insert(
                6,
                "Live inventory is reconciled from Kalshi positions; avg cost depends on portfolio payload fidelity.",
            )
        log.info("\n%s", "\n".join(lines))

    def run_once(self) -> bool:
        market = get_active_market(self.private_key)
        if market is None:
            log.info("No active Kalshi 15-minute market matched the filters.")
            return False
        self.book_state.reset_for_market(market["ticker"], market["_open_time"].timestamp())
        self.reconcile_live_state()
        spot = get_btc_price()
        if spot is None:
            return False
        orderbook = get_orderbook(self.private_key, str(market["ticker"]))
        if orderbook is None:
            return False
        snapshot = self.build_snapshot(market, orderbook, spot)
        if snapshot is None:
            log.warning("Snapshot build failed for %s", market["ticker"])
            return False
        self.maybe_accumulate(snapshot)
        self.maybe_hedge(snapshot)
        self.log_console(snapshot)
        self.save_state()
        return True

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
                time.sleep(SCAN_INTERVAL_SECONDS)
            except KeyboardInterrupt:
                self.save_state()
                self.save_trade_log()
                raise
            except Exception as exc:
                log.exception("Loop error: %s", exc)
                self.save_state()
                time.sleep(max(2.0, SCAN_INTERVAL_SECONDS))

    def save_trade_log(self) -> None:
        if not self.book_state.trade_log:
            return
        path = f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.book_state.trade_log, handle, indent=2)
        log.info("Trade log saved to %s", path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi 15-minute crypto dual-sided hedge bot.")
    parser.add_argument("--once", action="store_true", help="Run one decision cycle and exit.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    bot = DualSideKalshiBot()
    if args.once:
        bot.run_once()
        bot.save_trade_log()
        return 0
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
