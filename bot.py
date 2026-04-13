"""
Kalshi BTC 15-min Fair Value Bot - V1
======================================
Strategy:
  1. Pull BTC price from Binance (free, no key needed)
  2. Pull Kalshi order book for the active BTC 15-min contract
  3. Compute GBM fair value (binary option probability)
  4. If edge > threshold: post limit order
  5. Hard exit rules: never hold into last 2 minutes

Run in DEMO mode first. Switch DEMO=False only when backtested + paper traded.

Setup:
  pip install requests websockets cryptography python-dotenv

Auth:
  Kalshi uses RSA-PSS. Generate a key pair in your Kalshi account settings,
  download the private key, set the path in .env
"""

import os
import time
import math
import json
import base64
import asyncio
import logging
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

DEMO = True   # <-- ALWAYS start True. Set False only when ready for real money.

BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if DEMO else
    "https://trading-api.kalshi.com/trade-api/v2"
)

KALSHI_KEY_ID      = os.getenv("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY_PATH", "private_key.pem")

# Risk controls — do not change these until you have 50+ paper trades logged
EDGE_THRESHOLD     = 0.06   # only trade if model vs market gap > 6 cents
SPREAD_HALF        = 0.02   # post limit 2 cents inside fair value
MAX_CONTRACTS      = 5      # never hold more than 5 contracts at once
MIN_MINUTES_LEFT   = 2.0    # hard kill: no new orders inside last 2 min
STOP_LOSS_CENTS    = 8      # exit if position moves 8 cents against us
BTC_ANNUAL_VOL     = 0.65   # 65% annualized vol — update weekly from market data

# Kalshi BTC 15-min series ticker prefix (filter markets by this)
BTC_SERIES = "KXBTC"

# Binance endpoint for BTC spot price (free, no key)
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _load_private_key():
    with open(KALSHI_PRIVATE_KEY, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)

def _sign(private_key, method: str, path: str) -> dict:
    """Generate RSA-PSS signed headers for Kalshi API."""
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
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


# ─── Data fetchers ────────────────────────────────────────────────────────────

def get_btc_price() -> float:
    """Fetch BTC/USDT spot price from Binance."""
    try:
        r = requests.get(BINANCE_URL, timeout=3)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log.error(f"BTC price fetch failed: {e}")
        return None

def get_active_btc_market(private_key) -> dict | None:
    """
    Find the currently active BTC 15-min Kalshi contract.
    Returns the market dict with ticker, expiry, yes_ask, yes_bid.
    """
    path = "/markets"
    params = {"series_ticker": BTC_SERIES, "status": "open", "limit": 10}
    headers = _sign(private_key, "GET", path)
    try:
        r = requests.get(BASE_URL + path, headers=headers, params=params, timeout=5)
        r.raise_for_status()
        markets = r.json().get("markets", [])
        # Filter to 15-min contracts (close_time within 15 min of open_time)
        now = datetime.now(timezone.utc)
        for m in markets:
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            mins_left = (close - now).total_seconds() / 60
            if 0 < mins_left <= 15:
                m["_mins_left"] = mins_left
                return m
    except Exception as e:
        log.error(f"Market fetch failed: {e}")
    return None

def get_orderbook(private_key, ticker: str) -> dict | None:
    """Fetch order book for a specific market ticker."""
    path = f"/markets/{ticker}/orderbook"
    headers = _sign(private_key, "GET", path)
    try:
        r = requests.get(BASE_URL + path, headers=headers, timeout=5)
        r.raise_for_status()
        return r.json().get("orderbook", {})
    except Exception as e:
        log.error(f"Orderbook fetch failed: {e}")
    return None

def get_positions(private_key) -> list:
    """Get current open positions."""
    path = "/portfolio/positions"
    headers = _sign(private_key, "GET", path)
    try:
        r = requests.get(BASE_URL + path, headers=headers, timeout=5)
        r.raise_for_status()
        return r.json().get("market_positions", [])
    except Exception as e:
        log.error(f"Position fetch failed: {e}")
    return []


# ─── GBM Fair Value Model ─────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via Abramowitz & Stegun approximation."""
    a = [0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429]
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x = abs(x) / math.sqrt(2)
    t = 1 / (1 + p * x)
    y = 1 - (((((a[4]*t + a[3])*t + a[2])*t + a[1])*t + a[0])*t) * math.exp(-x*x)
    return 0.5 * (1 + sign * y)

def gbm_fair_value(
    btc_price: float,
    strike: float,
    minutes_left: float,
    annual_vol: float = BTC_ANNUAL_VOL,
) -> float:
    """
    Binary call option probability via GBM (risk-neutral).
    Returns probability [0, 1] that BTC closes ABOVE strike.

    This is the d2 term from Black-Scholes on a binary option:
      P(YES) = N(d2)
      d2 = [ln(S/K) - 0.5*σ²*t] / (σ*√t)
    """
    if minutes_left <= 0:
        return 1.0 if btc_price > strike else 0.0

    t = minutes_left / (365 * 24 * 60)   # convert to years
    sigma = annual_vol

    if sigma <= 0 or t <= 0:
        return 1.0 if btc_price > strike else 0.0

    d2 = (math.log(btc_price / strike) - 0.5 * sigma**2 * t) / (sigma * math.sqrt(t))
    return _norm_cdf(d2)

def parse_strike_from_ticker(ticker: str) -> float | None:
    """
    Extract strike price from Kalshi ticker.
    Example: KXBTC-25APR11-B83000 -> 83000.0
    Format: KXBTC-YYMONDD-B{strike}
    """
    try:
        parts = ticker.split("-")
        for p in parts:
            if p.startswith("B") and p[1:].isdigit():
                return float(p[1:])
    except Exception:
        pass
    log.warning(f"Could not parse strike from ticker: {ticker}")
    return None

def parse_market_price(orderbook: dict) -> tuple[float, float] | tuple[None, None]:
    """
    Extract best bid and ask from orderbook.
    Kalshi prices are now dollar strings e.g. "0.6500" (post March 2026 migration).
    Returns (bid, ask) as floats in [0, 1].
    """
    try:
        yes_bids = orderbook.get("yes", [])
        yes_asks = orderbook.get("no", [])   # no side = yes ask

        # Bids are sorted descending, asks ascending
        best_bid = float(yes_bids[0]["price"]) if yes_bids else None
        best_ask = float(yes_asks[0]["price"]) if yes_asks else None

        # Kalshi "no" side price is 1 - yes_ask
        if best_ask is not None:
            best_ask = 1.0 - best_ask

        return best_bid, best_ask
    except Exception as e:
        log.error(f"Price parse error: {e}")
        return None, None


# ─── Order execution ──────────────────────────────────────────────────────────

def place_limit_order(
    private_key,
    ticker: str,
    side: str,        # "yes" or "no"
    price_cents: int, # integer cents e.g. 45 for $0.45
    count: int,
) -> dict | None:
    """Place a limit order. Returns order dict or None on failure."""
    path = "/portfolio/orders"
    headers = _sign(private_key, "POST", path)

    # Post-March 2026: price as 4-decimal dollar string
    price_str = f"{price_cents / 100:.4f}"

    body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count": count,
        "yes_price": price_str if side == "yes" else None,
        "no_price": price_str if side == "no" else None,
        "time_in_force": "gtc",
    }
    body = {k: v for k, v in body.items() if v is not None}

    log.info(f"ORDER  {side.upper()} {count}x {ticker} @ {price_cents}¢")

    if DEMO:
        # In demo, still send the real request — Kalshi demo accepts real orders
        pass

    try:
        r = requests.post(BASE_URL + path, headers=headers, json=body, timeout=5)
        r.raise_for_status()
        return r.json().get("order")
    except Exception as e:
        log.error(f"Order failed: {e} — {getattr(r, 'text', '')}")
    return None

def cancel_order(private_key, order_id: str) -> bool:
    """Cancel an open order by ID."""
    path = f"/portfolio/orders/{order_id}"
    headers = _sign(private_key, "DELETE", path)
    try:
        r = requests.delete(BASE_URL + path, headers=headers, timeout=5)
        r.raise_for_status()
        log.info(f"CANCEL order {order_id}")
        return True
    except Exception as e:
        log.error(f"Cancel failed: {e}")
    return False


# ─── Fee calculator ───────────────────────────────────────────────────────────

def kalshi_fee(price: float, contracts: int) -> float:
    """
    Kalshi taker fee formula: fee = 0.07 * P * (1 - P) per contract
    where P is the yes price in dollars.
    """
    return 0.07 * price * (1 - price) * contracts


# ─── Main loop ────────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.position_ticker = None   # ticker we're currently holding
        self.position_side   = None   # "yes" or "no"
        self.position_size   = 0      # number of contracts
        self.entry_price     = None   # price we entered at
        self.open_orders     = []     # list of order IDs we've placed
        self.trades_log      = []     # every trade for analysis later

    def has_position(self):
        return self.position_size > 0

    def log_trade(self, event: str, **kwargs):
        entry = {"time": datetime.now().isoformat(), "event": event, **kwargs}
        self.trades_log.append(entry)
        log.info(f"TRADE  {event}  {kwargs}")


def run_bot():
    log.info(f"{'='*50}")
    log.info(f"Kalshi BTC 15-min Bot  |  DEMO={DEMO}")
    log.info(f"Edge threshold: {EDGE_THRESHOLD*100:.0f}¢  |  Max contracts: {MAX_CONTRACTS}")
    log.info(f"{'='*50}")

    if not KALSHI_KEY_ID:
        log.error("KALSHI_KEY_ID not set in .env — exiting")
        return

    private_key = _load_private_key()
    state = BotState()

    while True:
        try:
            loop_start = time.time()

            # ── 1. Get BTC price ──────────────────────────────────────────
            btc = get_btc_price()
            if btc is None:
                time.sleep(5)
                continue

            # ── 2. Find active market ─────────────────────────────────────
            market = get_active_btc_market(private_key)
            if market is None:
                log.info("No active BTC 15-min market found — waiting...")
                time.sleep(15)
                continue

            ticker    = market["ticker"]
            mins_left = market["_mins_left"]
            strike    = parse_strike_from_ticker(ticker)

            if strike is None:
                log.warning(f"Skipping {ticker} — could not parse strike")
                time.sleep(10)
                continue

            # ── 3. Compute fair value ─────────────────────────────────────
            fair = gbm_fair_value(btc, strike, mins_left)
            fee  = kalshi_fee(fair, 1)

            # ── 4. Get order book ─────────────────────────────────────────
            ob = get_orderbook(private_key, ticker)
            if ob is None:
                time.sleep(5)
                continue

            bid, ask = parse_market_price(ob)

            log.info(
                f"BTC=${btc:,.0f}  strike=${strike:,.0f}  "
                f"t={mins_left:.1f}min  fair={fair*100:.1f}¢  "
                f"bid={bid*100 if bid else '?':.0f}¢  ask={ask*100 if ask else '?':.0f}¢"
            )

            # ── 5. Hard time gate ─────────────────────────────────────────
            if mins_left < MIN_MINUTES_LEFT:
                log.warning(f"TIME GATE: {mins_left:.1f} min left — no new orders")
                if state.has_position():
                    log.warning("FORCE EXIT: time gate triggered with open position")
                    # TODO: implement market sell here
                time.sleep(10)
                continue

            # ── 6. Position management (if we hold something) ─────────────
            if state.has_position() and state.position_ticker == ticker:
                if state.position_side == "yes" and bid is not None:
                    pnl_cents = (bid - state.entry_price) * 100
                    if pnl_cents >= 5:
                        log.info(f"TAKE PROFIT: +{pnl_cents:.1f}¢")
                        # TODO: place sell order
                    elif pnl_cents <= -STOP_LOSS_CENTS:
                        log.warning(f"STOP LOSS: {pnl_cents:.1f}¢")
                        # TODO: place sell order

            # ── 7. Signal generation ──────────────────────────────────────
            if state.has_position():
                time.sleep(10)
                continue   # already in a trade, just monitor

            edge_vs_ask = fair - (ask or 1.0)   # positive = market underpricing YES
            edge_vs_bid = (bid or 0.0) - fair   # positive = market overpricing YES

            # Effective edge after fees
            net_edge_buy  = edge_vs_ask - fee
            net_edge_sell = edge_vs_bid - fee

            if ask is not None and net_edge_buy > EDGE_THRESHOLD:
                # Market underpricing YES — buy YES
                limit_price = round((fair - SPREAD_HALF) * 100)
                limit_price = max(1, min(99, limit_price))
                order = place_limit_order(private_key, ticker, "yes", limit_price, 1)
                if order:
                    state.position_ticker = ticker
                    state.position_side   = "yes"
                    state.position_size   = 1
                    state.entry_price     = limit_price / 100
                    state.log_trade("BUY_YES", ticker=ticker, price=limit_price,
                                    fair=round(fair*100, 1), edge=round(net_edge_buy*100, 1))

            elif bid is not None and net_edge_sell > EDGE_THRESHOLD:
                # Market overpricing YES — buy NO (equivalent to selling YES)
                limit_price = round((1 - fair - SPREAD_HALF) * 100)
                limit_price = max(1, min(99, limit_price))
                order = place_limit_order(private_key, ticker, "no", limit_price, 1)
                if order:
                    state.position_ticker = ticker
                    state.position_side   = "no"
                    state.position_size   = 1
                    state.entry_price     = limit_price / 100
                    state.log_trade("BUY_NO", ticker=ticker, price=limit_price,
                                    fair=round(fair*100, 1), edge=round(net_edge_sell*100, 1))
            else:
                log.info(f"HOLD  net_edge_buy={net_edge_buy*100:.1f}¢  net_edge_sell={net_edge_sell*100:.1f}¢  (threshold={EDGE_THRESHOLD*100:.0f}¢)")

            # ── 8. Loop pacing ─────────────────────────────────────────────
            elapsed = time.time() - loop_start
            sleep_for = max(0, 10 - elapsed)   # target 10-second loop
            time.sleep(sleep_for)

        except KeyboardInterrupt:
            log.info("Bot stopped by user")
            _save_log(state)
            break
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            time.sleep(15)


def _save_log(state: BotState):
    fname = f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(fname, "w") as f:
        json.dump(state.trades_log, f, indent=2)
    log.info(f"Trade log saved to {fname}")


if __name__ == "__main__":
    run_bot()