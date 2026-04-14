#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


KALSHI_REST_PROD = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_REST_DEMO = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_WS_PROD = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_WS_DEMO = "wss://demo-api.kalshi.co/trade-api/ws/v2"
POLY_WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", os.getenv("KALSHI_KEY_ID", ""))
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "private_key.pem")


def now_ms() -> int:
    return int(time.time() * 1000)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def clear_terminal() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def load_private_key():
    with open(KALSHI_PRIVATE_KEY_PATH, "rb") as handle:
        return serialization.load_pem_private_key(handle.read(), password=None)


def kalshi_ws_headers() -> Optional[dict[str, str]]:
    if not KALSHI_API_KEY_ID or not os.path.exists(KALSHI_PRIVATE_KEY_PATH):
        return None
    private_key = load_private_key()
    ts = str(int(time.time() * 1000))
    path = "/trade-api/ws/v2"
    message = ts + "GET" + path
    signature = private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


@dataclass(frozen=True)
class MarketPair:
    label: str
    kalshi_ticker: str
    polymarket_yes_asset_id: str
    polymarket_no_asset_id: str
    notes: str = ""


@dataclass
class VenueQuote:
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    last_update_ms: int = 0

    @property
    def ready(self) -> bool:
        return None not in {self.yes_bid, self.yes_ask, self.no_bid, self.no_ask}

    @property
    def yes_mid(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def no_mid(self) -> Optional[float]:
        if self.no_bid is None or self.no_ask is None:
            return None
        return (self.no_bid + self.no_ask) / 2.0


@dataclass
class PairState:
    pair: MarketPair
    kalshi: VenueQuote = field(default_factory=VenueQuote)
    polymarket: VenueQuote = field(default_factory=VenueQuote)
    last_opportunity: str = ""
    last_opportunity_value: float = 0.0
    alerts_triggered: int = 0


@dataclass(frozen=True)
class Opportunity:
    kind: str
    spread: float
    buy_venue: str
    buy_side: str
    buy_price: float
    sell_venue: Optional[str] = None
    sell_side: Optional[str] = None
    sell_price: Optional[float] = None


class ScannerState:
    def __init__(self, pairs: list[MarketPair], arb_threshold_cents: float):
        self.pairs = pairs
        self.pairs_by_label = {pair.label: PairState(pair=pair) for pair in pairs}
        self.pairs_by_kalshi = {pair.kalshi_ticker: self.pairs_by_label[pair.label] for pair in pairs}
        self.pairs_by_poly_asset: dict[str, PairState] = {}
        for pair in pairs:
            state = self.pairs_by_label[pair.label]
            self.pairs_by_poly_asset[pair.polymarket_yes_asset_id] = state
            self.pairs_by_poly_asset[pair.polymarket_no_asset_id] = state
        self.arb_threshold = arb_threshold_cents / 100.0
        self.started_at_ms = now_ms()
        self.last_render_ms = 0

    def best_opportunities(self) -> list[tuple[PairState, Opportunity]]:
        rows: list[tuple[PairState, Opportunity]] = []
        for state in self.pairs_by_label.values():
            opportunity = detect_best_opportunity(state)
            if opportunity is not None:
                rows.append((state, opportunity))
        rows.sort(key=lambda row: row[1].spread, reverse=True)
        return rows


def load_market_pairs(path: str) -> list[MarketPair]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("market pair config must be a JSON array")
    pairs: list[MarketPair] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each market pair must be an object")
        pairs.append(
            MarketPair(
                label=str(item["label"]),
                kalshi_ticker=str(item["kalshi_ticker"]),
                polymarket_yes_asset_id=str(item["polymarket_yes_asset_id"]),
                polymarket_no_asset_id=str(item["polymarket_no_asset_id"]),
                notes=str(item.get("notes", "")),
            )
        )
    return pairs


def detect_best_opportunity(state: PairState) -> Optional[Opportunity]:
    k = state.kalshi
    p = state.polymarket
    if not (k.ready and p.ready):
        return None

    candidates = [
        Opportunity(
            kind="cross_yes",
            spread=(k.yes_bid or 0.0) - (p.yes_ask or 1.0),
            buy_venue="polymarket",
            buy_side="YES",
            buy_price=p.yes_ask or 0.0,
            sell_venue="kalshi",
            sell_side="YES",
            sell_price=k.yes_bid,
        ),
        Opportunity(
            kind="cross_yes",
            spread=(p.yes_bid or 0.0) - (k.yes_ask or 1.0),
            buy_venue="kalshi",
            buy_side="YES",
            buy_price=k.yes_ask or 0.0,
            sell_venue="polymarket",
            sell_side="YES",
            sell_price=p.yes_bid,
        ),
        Opportunity(
            kind="cross_no",
            spread=(k.no_bid or 0.0) - (p.no_ask or 1.0),
            buy_venue="polymarket",
            buy_side="NO",
            buy_price=p.no_ask or 0.0,
            sell_venue="kalshi",
            sell_side="NO",
            sell_price=k.no_bid,
        ),
        Opportunity(
            kind="cross_no",
            spread=(p.no_bid or 0.0) - (k.no_ask or 1.0),
            buy_venue="kalshi",
            buy_side="NO",
            buy_price=k.no_ask or 0.0,
            sell_venue="polymarket",
            sell_side="NO",
            sell_price=p.no_bid,
        ),
        Opportunity(
            kind="synthetic_lock_yes_k_no_p",
            spread=1.0 - ((k.yes_ask or 1.0) + (p.no_ask or 1.0)),
            buy_venue="kalshi",
            buy_side="YES",
            buy_price=k.yes_ask or 0.0,
            sell_venue="polymarket",
            sell_side="NO",
            sell_price=p.no_ask,
        ),
        Opportunity(
            kind="synthetic_lock_no_k_yes_p",
            spread=1.0 - ((k.no_ask or 1.0) + (p.yes_ask or 1.0)),
            buy_venue="kalshi",
            buy_side="NO",
            buy_price=k.no_ask or 0.0,
            sell_venue="polymarket",
            sell_side="YES",
            sell_price=p.yes_ask,
        ),
        Opportunity(
            kind="synthetic_lock_yes_p_no_k",
            spread=1.0 - ((p.yes_ask or 1.0) + (k.no_ask or 1.0)),
            buy_venue="polymarket",
            buy_side="YES",
            buy_price=p.yes_ask or 0.0,
            sell_venue="kalshi",
            sell_side="NO",
            sell_price=k.no_ask,
        ),
        Opportunity(
            kind="synthetic_lock_no_p_yes_k",
            spread=1.0 - ((p.no_ask or 1.0) + (k.yes_ask or 1.0)),
            buy_venue="polymarket",
            buy_side="NO",
            buy_price=p.no_ask or 0.0,
            sell_venue="kalshi",
            sell_side="YES",
            sell_price=k.yes_ask,
        ),
    ]
    candidates.sort(key=lambda item: item.spread, reverse=True)
    return candidates[0]


def render_tui(state: ScannerState) -> str:
    uptime_seconds = max(1, (now_ms() - state.started_at_ms) // 1000)
    header = [
        "Cross-Exchange Arb Scanner",
        f"Pairs: {len(state.pairs)} | Uptime: {uptime_seconds}s | Threshold: {state.arb_threshold * 100:.0f}c",
        "",
    ]
    lines: list[str] = []
    opportunities = state.best_opportunities()
    if not opportunities:
        lines.append("No fully-priced matched pairs yet.")
    else:
        for pair_state, opportunity in opportunities:
            mark = "ARB DETECTED" if opportunity.spread >= state.arb_threshold else "watch"
            lines.append(
                f"[{mark}] {pair_state.pair.label} | kind={opportunity.kind} | spread={opportunity.spread * 100:.1f}c"
            )
            lines.append(
                f"  buy {opportunity.buy_side} on {opportunity.buy_venue} @ {opportunity.buy_price:.3f}"
            )
            if opportunity.sell_venue and opportunity.sell_side and opportunity.sell_price is not None:
                lines.append(
                    f"  hedge/sell {opportunity.sell_side} on {opportunity.sell_venue} @ {opportunity.sell_price:.3f}"
                )
            lines.append(
                f"  kalshi Y {pair_state.kalshi.yes_bid} / {pair_state.kalshi.yes_ask} | "
                f"N {pair_state.kalshi.no_bid} / {pair_state.kalshi.no_ask}"
            )
            lines.append(
                f"  poly    Y {pair_state.polymarket.yes_bid} / {pair_state.polymarket.yes_ask} | "
                f"N {pair_state.polymarket.no_bid} / {pair_state.polymarket.no_ask}"
            )
            if pair_state.pair.notes:
                lines.append(f"  notes: {pair_state.pair.notes}")
            lines.append("")
    return "\n".join(header + lines)


async def tui_loop(state: ScannerState, refresh_ms: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        clear_terminal()
        print(render_tui(state))
        await asyncio.sleep(refresh_ms / 1000.0)


class KalshiFeed:
    def __init__(self, state: ScannerState, env: str):
        self.state = state
        self.ws_url = KALSHI_WS_DEMO if env == "demo" else KALSHI_WS_PROD
        self.headers = kalshi_ws_headers()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                connect_kwargs: dict[str, Any] = {"ping_interval": 20, "ping_timeout": 20}
                if self.headers:
                    connect_kwargs["additional_headers"] = self.headers
                async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "id": 1,
                                "cmd": "subscribe",
                                "params": {"channels": ["ticker"]},
                            }
                        )
                    )
                    async for raw in ws:
                        if stop_event.is_set():
                            return
                        self.process_message(raw)
            except Exception:
                await asyncio.sleep(2.0)

    def process_message(self, raw: str) -> None:
        payload = json.loads(raw)
        if payload.get("type") != "ticker":
            return
        msg = payload.get("msg") or {}
        ticker = str(msg.get("market_ticker", ""))
        pair_state = self.state.pairs_by_kalshi.get(ticker)
        if pair_state is None:
            return
        pair_state.kalshi.yes_bid = to_dollar(msg.get("yes_bid"))
        pair_state.kalshi.yes_ask = to_dollar(msg.get("yes_ask"))
        pair_state.kalshi.no_bid = to_dollar(msg.get("no_bid"))
        pair_state.kalshi.no_ask = to_dollar(msg.get("no_ask"))
        pair_state.kalshi.last_update_ms = now_ms()


class PolymarketFeed:
    def __init__(self, state: ScannerState):
        self.state = state

    async def run(self, stop_event: asyncio.Event) -> None:
        asset_ids = list(self.state.pairs_by_poly_asset.keys())
        while not stop_event.is_set():
            try:
                async with websockets.connect(POLY_WS_MARKET, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "assets_ids": asset_ids,
                                "type": "market",
                                "custom_feature_enabled": True,
                            }
                        )
                    )
                    async for raw in ws:
                        if stop_event.is_set():
                            return
                        self.process_message(raw)
            except Exception:
                await asyncio.sleep(2.0)

    def process_message(self, raw: str) -> None:
        payload = json.loads(raw)
        event_type = payload.get("event_type")
        asset_id = str(payload.get("asset_id", ""))
        pair_state = self.state.pairs_by_poly_asset.get(asset_id)
        if pair_state is None:
            return
        is_yes = asset_id == pair_state.pair.polymarket_yes_asset_id
        quote = pair_state.polymarket
        if event_type == "book":
            bids = payload.get("bids") or []
            asks = payload.get("asks") or []
            best_bid = max((float(item["price"]) for item in bids), default=None)
            best_ask = min((float(item["price"]) for item in asks), default=None)
            self._update_side(quote, is_yes, best_bid, best_ask)
        elif event_type == "price_change":
            changes = payload.get("changes") or []
            prices = [float(item["price"]) for item in changes if "price" in item]
            if not prices:
                return
            best_bid = quote.yes_bid if is_yes else quote.no_bid
            best_ask = quote.yes_ask if is_yes else quote.no_ask
            if payload.get("side") == "BUY":
                best_bid = max(prices)
            elif payload.get("side") == "SELL":
                best_ask = min(prices)
            self._update_side(quote, is_yes, best_bid, best_ask)
        elif event_type == "best_bid_ask":
            best_bid = parse_optional_float(payload.get("best_bid"))
            best_ask = parse_optional_float(payload.get("best_ask"))
            self._update_side(quote, is_yes, best_bid, best_ask)

    def _update_side(self, quote: VenueQuote, is_yes: bool, bid: Optional[float], ask: Optional[float]) -> None:
        if is_yes:
            quote.yes_bid = bid
            quote.yes_ask = ask
        else:
            quote.no_bid = bid
            quote.no_ask = ask
        quote.last_update_ms = now_ms()


def parse_optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_dollar(value: Any) -> Optional[float]:
    parsed = parse_optional_float(value)
    if parsed is None:
        return None
    return parsed / 100.0 if parsed > 1.0 else parsed


class ExecutionStub:
    async def execute(self, pair_state: PairState, opportunity: Opportunity) -> None:
        raise NotImplementedError(
            "Scanner only. Add authenticated Kalshi IOC/GTC order placement and "
            "Polymarket signed CLOB order placement here."
        )


async def alert_loop(
    state: ScannerState,
    execution: ExecutionStub,
    poll_ms: int,
    stop_event: asyncio.Event,
) -> None:
    seen: dict[str, tuple[str, int]] = {}
    while not stop_event.is_set():
        for pair_state, opportunity in state.best_opportunities():
            if opportunity.spread < state.arb_threshold:
                continue
            signature = (
                f"{opportunity.kind}:{opportunity.buy_venue}:{opportunity.buy_side}:"
                f"{opportunity.sell_venue}:{opportunity.sell_side}"
            )
            last = seen.get(pair_state.pair.label)
            current_bucket = int(opportunity.spread * 1000)
            if last == (signature, current_bucket):
                continue
            seen[pair_state.pair.label] = (signature, current_bucket)
            pair_state.last_opportunity = signature
            pair_state.last_opportunity_value = opportunity.spread
            pair_state.alerts_triggered += 1
            print(
                f"ARB DETECTED | {pair_state.pair.label} | {opportunity.kind} | "
                f"{opportunity.spread * 100:.1f}c | buy {opportunity.buy_side} on {opportunity.buy_venue}"
            )
            # Scanner only for now. This is where automated execution would be called.
            # await execution.execute(pair_state, opportunity)
        await asyncio.sleep(poll_ms / 1000.0)


def validate_kalshi_pairs(pairs: list[MarketPair], env: str) -> None:
    base_url = KALSHI_REST_DEMO if env == "demo" else KALSHI_REST_PROD
    for pair in pairs:
        try:
            response = requests.get(f"{base_url}/markets/{pair.kalshi_ticker}", timeout=5)
            if response.status_code >= 400:
                print(f"warning: kalshi ticker validation failed for {pair.kalshi_ticker}: {response.status_code}")
        except Exception as exc:
            print(f"warning: kalshi ticker validation failed for {pair.kalshi_ticker}: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi/Polymarket exact-match arbitrage scanner.")
    parser.add_argument("--config", default="market_pairs.json", help="Exact market pair config JSON.")
    parser.add_argument("--kalshi-env", choices=["prod", "demo"], default="prod")
    parser.add_argument("--arb-threshold-cents", type=float, default=10.0)
    parser.add_argument("--refresh-ms", type=int, default=250)
    parser.add_argument("--validate-kalshi", action="store_true")
    return parser


async def async_main(args: argparse.Namespace) -> int:
    pairs = load_market_pairs(args.config)
    if args.validate_kalshi:
        validate_kalshi_pairs(pairs, args.kalshi_env)
    state = ScannerState(pairs=pairs, arb_threshold_cents=args.arb_threshold_cents)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(KalshiFeed(state, args.kalshi_env).run(stop_event)),
        asyncio.create_task(PolymarketFeed(state).run(stop_event)),
        asyncio.create_task(tui_loop(state, args.refresh_ms, stop_event)),
        asyncio.create_task(alert_loop(state, ExecutionStub(), 100, stop_event)),
    ]

    try:
        await stop_event.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
