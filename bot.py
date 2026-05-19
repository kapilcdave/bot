#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


MINUTE_WINDOW_START = env_float("KALSHI_WINDOW_START_MINUTE", 2.0)
MINUTE_WINDOW_END = env_float("KALSHI_WINDOW_END_MINUTE", 11.0)
MIN_EDGE = env_float("KALSHI_MIN_MISPRICING_EDGE", 0.10)
BANKROLL_DOLLARS = env_float("KALSHI_BANKROLL_DOLLARS", 20.0)
LOOP_SECONDS = env_float("KALSHI_LOOP_SECONDS", 2.0)


@dataclass(frozen=True)
class MarketPricing:
    yes_price: float
    no_price: float


class MidMarketSniperBot:
    """
    Mid-window Kalshi sniper:
    - scans only between minute 2 and minute 11 of the current market
    - compares Kalshi NO price against institutional Deribit-derived NO probability
    - buys NO when edge exceeds configured threshold
    """

    def get_deribit_true_probability(self) -> float:
        """
        Placeholder for Deribit options-implied probability extraction.
        Replace this with live Deribit IV/delta logic.
        """
        return 0.42

    def get_kalshi_market_prices(self) -> MarketPricing:
        """
        Placeholder for live Kalshi orderbook call.
        Replace with API-derived YES/NO prices.
        """
        yes_price = 0.58
        no_price = 0.42
        return MarketPricing(yes_price=yes_price, no_price=no_price)

    def minute_of_current_market(self, market_open_utc: datetime) -> float:
        now_utc = datetime.now(timezone.utc)
        return max(0.0, (now_utc - market_open_utc).total_seconds() / 60.0)

    def in_trading_window(self, minute_in_market: float) -> bool:
        return MINUTE_WINDOW_START <= minute_in_market <= MINUTE_WINDOW_END

    def check_kalshi_mispricing(self) -> str:
        prices = self.get_kalshi_market_prices()
        true_yes_prob = self.get_deribit_true_probability()
        true_no_prob = 1.0 - true_yes_prob

        if (true_no_prob - prices.no_price) > MIN_EDGE:
            log.info(
                "Edge Found: true_no=%.2f kalshi_no=%.2f edge=%.2f",
                true_no_prob,
                prices.no_price,
                true_no_prob - prices.no_price,
            )
            return "BUY_NO"
        return "HOLD"

    def execute_snipe(self, side: str) -> None:
        log.info("Executing $%.2f snipe order on side=%s", BANKROLL_DOLLARS, side)
        # Replace with real Kalshi order submission code.

    def run_once(self, market_open_utc: datetime) -> None:
        minute_in_market = self.minute_of_current_market(market_open_utc)
        if not self.in_trading_window(minute_in_market):
            log.info(
                "Outside trading window (minute=%.2f, window=%.2f-%.2f): HOLD",
                minute_in_market,
                MINUTE_WINDOW_START,
                MINUTE_WINDOW_END,
            )
            return

        action = self.check_kalshi_mispricing()
        if action == "BUY_NO":
            self.execute_snipe("NO")
        else:
            log.info("No edge: HOLD")

    def run_forever(self, market_open_utc: datetime) -> None:
        while True:
            self.run_once(market_open_utc)
            time.sleep(LOOP_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BTC Kalshi mid-market mispricing sniper.")
    parser.add_argument(
        "--market-open-utc",
        default=None,
        help="ISO timestamp for market open (e.g., 2025-05-18T23:00:00+00:00). Defaults to current time.",
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    return parser


def parse_market_open(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    args = build_parser().parse_args()
    market_open_utc = parse_market_open(args.market_open_utc)
    bot = MidMarketSniperBot()

    if args.once:
        bot.run_once(market_open_utc)
        return 0

    try:
        bot.run_forever(market_open_utc)
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
