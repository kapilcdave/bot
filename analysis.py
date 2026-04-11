#!/usr/bin/env python3
"""
kalshi_15min_scalper_analysis.py

Production-ready, single-file analyzer for Kalshi 15-minute crypto up/down
binary markets, focused on an expiry-scalping workflow.

What it does:
1. Pulls historical market metadata for selected Kalshi crypto 15-minute series.
2. Pulls 1-minute YES-price candlesticks for each market via the public API.
3. Reconstructs the last available YES price at exact pre-expiry windows:
   T-5m, T-4m, T-3m, T-2m, T-90s, T-60s.
4. Evaluates "favorite-side" scalps:
   - If YES price >= 50c, favorite is YES at price = yes_price.
   - Otherwise favorite is NO at price = 1 - yes_price.
5. Computes realized win rates, EV, liquidity/tradeability signals, upset rates,
   bankroll simulations, per-coin rankings, and a final strategy recommendation.

Assumptions:
- Kalshi returns market settlement via the market's "result" field.
- Settlement payout is $1.00 per winning contract.
- The fee model requested by the user is approximated as a 7% fee on winnings
  only, so winning profit is discounted by (1 - fee_rate).
- Historical tradeability is proxied from public candlesticks: when the favorite
  side costs < 0.99 and a valid price exists, we assume a market was still
  tradeable.

Run:
    python kalshi_15min_scalper_analysis.py

Dependencies:
    pip install requests pandas tqdm matplotlib seaborn
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "matplotlib"))

try:
    import matplotlib.pyplot as plt
    import seaborn as sns
except ModuleNotFoundError:
    plt = None
    sns = None


# ============================================================================
# Configuration
# ============================================================================

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_SLEEP_SECONDS = 0.3
REQUEST_TIMEOUT_SECONDS = 25
RETRY_TOTAL = 5
RETRY_BACKOFF_FACTOR = 0.7

COIN_SERIES: Dict[str, str] = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "SOL": "KXSOL15M",
    "XRP": "KXXRP15M",
    "BNB": "KXBNB15M",
    "DOGE": "KXDOGE15M",
    "HYPE": "KXHYPE15M",
}

TIME_WINDOWS: List[Tuple[str, int]] = [
    ("T-5m", 300),
    ("T-4m", 240),
    ("T-3m", 180),
    ("T-2m", 120),
    ("T-90s", 90),
    ("T-60s", 60),
]

PRICE_THRESHOLDS_CENTS: List[int] = [88, 90, 92, 94, 96, 98]
FEE_RATE = 0.07
MAX_MARKETS_PER_COIN = 300
PER_PAGE_LIMIT = 200

STARTING_BANKROLL = 100.0
BANKROLL_RISK_FRACTION = 0.25
BANKROLL_SIM_TRADES = 300

TRADEABLE_MAX_PRICE = 0.99
DRY_UP_PRICE = 0.99
UPSET_THRESHOLD = 0.96

CHART_COINS = ["BTC", "ETH"]
CHART_THRESHOLD_CENTS = 90


# ============================================================================
# Data classes
# ============================================================================

@dataclass
class WindowObservation:
    """Container for one market at one exact pre-expiry observation window."""

    coin: str
    series_ticker: str
    market_ticker: str
    close_time: pd.Timestamp
    result: str
    window_label: str
    window_seconds: int
    cutoff_time: pd.Timestamp
    observed_ts: Optional[pd.Timestamp]
    yes_price: Optional[float]
    favorite_side: Optional[str]
    favorite_price: Optional[float]
    winning_side: Optional[bool]
    won: Optional[bool]
    pnl_per_contract: Optional[float]
    ev_per_dollar_risked: Optional[float]
    tradeable: bool
    dried_up_by_window: Optional[bool]


# ============================================================================
# HTTP / parsing helpers
# ============================================================================

def build_session() -> requests.Session:
    """
    Build a retrying requests session suitable for public API polling.
    """
    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "kalshi-15min-scalper-analysis/1.0",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion returning None on malformed input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_timestamp(value: Any) -> Optional[pd.Timestamp]:
    """
    Parse either ISO8601 timestamps or unix timestamps into UTC-aware pandas
    timestamps.
    """
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)) and not math.isnan(float(value)):
        # Kalshi candlesticks use unix timestamps in seconds.
        return pd.to_datetime(int(value), unit="s", utc=True)

    text = str(value).strip()
    if not text:
        return None

    try:
        return pd.to_datetime(text, utc=True)
    except Exception:
        return None


def market_result_normalized(raw_result: Any) -> Optional[str]:
    """
    Normalize Kalshi market settlement results to 'yes' / 'no' when possible.
    """
    if raw_result is None:
        return None

    result = str(raw_result).strip().lower()
    if result in {"yes", "true", "1"}:
        return "yes"
    if result in {"no", "false", "0"}:
        return "no"
    return None


def markdown_table(df: pd.DataFrame, max_rows: Optional[int] = None) -> str:
    """Convert a dataframe into a compact Markdown table for console output."""
    if df.empty:
        return "_No rows_"
    if max_rows is not None:
        df = df.head(max_rows)

    display_df = df.copy()
    display_df.columns = [str(col) for col in display_df.columns]
    for col in display_df.columns:
        display_df[col] = display_df[col].map(
            lambda x: "" if pd.isna(x) else str(x)
        )

    headers = list(display_df.columns)
    rows = display_df.values.tolist()
    widths = [
        max(len(headers[idx]), *(len(str(row[idx])) for row in rows)) if rows else len(headers[idx])
        for idx in range(len(headers))
    ]

    def fmt_row(values: Sequence[str]) -> str:
        return "| " + " | ".join(str(value).ljust(widths[idx]) for idx, value in enumerate(values)) + " |"

    separator = "| " + " | ".join("-" * width for width in widths) + " |"
    lines = [fmt_row(headers), separator]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def request_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    context: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Execute a GET request with retrying session semantics plus a fixed sleep to
    avoid hammering public endpoints.
    """
    try:
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        time.sleep(REQUEST_SLEEP_SECONDS)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object response.")
        return payload
    except Exception as exc:
        label = f" [{context}]" if context else ""
        print(f"Warning: request failed{label}: {url} -> {exc}", file=sys.stderr)
        return None


# ============================================================================
# Kalshi fetchers
# ============================================================================

def fetch_markets(
    session: requests.Session,
    *,
    series_ticker: str,
    max_markets: int,
) -> List[Dict[str, Any]]:
    """
    Fetch recent markets for one series ticker.

    Kalshi's current /markets endpoint accepts `series_ticker` plus either a
    single concrete `status` value or no status filter. `status=all` is not a
    valid value, and `/historical/markets` does not support `series_ticker`,
    so this fetcher sticks to valid `/markets` queries only.
    """
    collected: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    endpoint_variants = [
        (f"{BASE_URL}/markets", {"status": "settled"}),
        (f"{BASE_URL}/markets", {}),
    ]

    for endpoint, extra_params in endpoint_variants:
        local_rows: List[Dict[str, Any]] = []
        cursor = None

        while len(local_rows) < max_markets:
            params: Dict[str, Any] = {
                "series_ticker": series_ticker,
                "limit": min(PER_PAGE_LIMIT, max_markets - len(local_rows)),
            }
            params.update(extra_params)
            if cursor:
                params["cursor"] = cursor

            payload = request_json(
                session,
                endpoint,
                params=params,
                context=f"fetch_markets:{series_ticker}",
            )
            if payload is None:
                break

            markets = payload.get("markets") or []
            if not isinstance(markets, list):
                break

            local_rows.extend(markets)
            cursor = payload.get("cursor")
            if not cursor:
                break

        if local_rows:
            collected.extend(local_rows)

        if len(collected) >= max_markets:
            break

    deduped: Dict[str, Dict[str, Any]] = {}
    for market in collected:
        ticker = market.get("ticker")
        if ticker:
            deduped[ticker] = market

    rows = list(deduped.values())
    rows.sort(
        key=lambda m: safe_timestamp(m.get("close_time")) or pd.Timestamp.min.tz_localize("UTC"),
        reverse=True,
    )
    return rows[:max_markets]


def fetch_candlesticks(
    session: requests.Session,
    *,
    series_ticker: str,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
) -> List[Dict[str, Any]]:
    """
    Fetch 1-minute candlesticks for one market.

    The user requested:
        /markets/{ticker}/candlesticks
        fallback -> /historical/markets/{ticker}/candlesticks

    Kalshi's current docs also expose:
        /series/{series_ticker}/markets/{ticker}/candlesticks

    To maximize compatibility, this function tries all reasonable public
    variants in a strict order.
    """
    params = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": 1,
        "include_latest_before_start": "true",
    }

    candidate_urls = [
        f"{BASE_URL}/markets/{market_ticker}/candlesticks",
        f"{BASE_URL}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        f"{BASE_URL}/historical/markets/{market_ticker}/candlesticks",
        f"{BASE_URL}/historical/series/{series_ticker}/markets/{market_ticker}/candlesticks",
    ]

    for url in candidate_urls:
        payload = request_json(
            session,
            url,
            params=params,
            context=f"fetch_candlesticks:{market_ticker}",
        )
        if not payload:
            continue
        candles = payload.get("candlesticks") or []
        if isinstance(candles, list) and candles:
            return candles

    return []


# ============================================================================
# Market/candle transformation helpers
# ============================================================================

def normalize_candlesticks(raw_candles: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    """
    Normalize Kalshi candlestick payloads into a simple dataframe with one row
    per candle end timestamp and a YES midpoint/last-traded price.
    """
    records: List[Dict[str, Any]] = []

    for candle in raw_candles:
        end_ts = safe_timestamp(candle.get("end_period_ts"))
        price_block = candle.get("price") or {}
        yes_bid_block = candle.get("yes_bid") or {}
        yes_ask_block = candle.get("yes_ask") or {}

        # Prefer public midpoint/last-traded YES price. Fall back to bid/ask.
        price_candidates = [
            safe_float(price_block.get("close_dollars")),
            safe_float(price_block.get("mean_dollars")),
            safe_float(price_block.get("previous_dollars")),
        ]
        yes_bid = safe_float(yes_bid_block.get("close_dollars"))
        yes_ask = safe_float(yes_ask_block.get("close_dollars"))

        yes_price = next((p for p in price_candidates if p is not None), None)
        if yes_price is None and yes_bid is not None and yes_ask is not None:
            yes_price = (yes_bid + yes_ask) / 2.0
        elif yes_price is None and yes_bid is not None:
            yes_price = yes_bid
        elif yes_price is None and yes_ask is not None:
            yes_price = yes_ask

        records.append(
            {
                "end_period_ts": end_ts,
                "yes_price": yes_price,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "volume_fp": safe_float(candle.get("volume_fp")),
                "open_interest_fp": safe_float(candle.get("open_interest_fp")),
            }
        )

    df = pd.DataFrame.from_records(records)
    if df.empty:
        return pd.DataFrame(
            columns=["end_period_ts", "yes_price", "yes_bid", "yes_ask", "volume_fp", "open_interest_fp"]
        )

    df = df.dropna(subset=["end_period_ts"]).sort_values("end_period_ts").reset_index(drop=True)
    return df


def favorite_from_yes_price(yes_price: Optional[float]) -> Tuple[Optional[str], Optional[float]]:
    """
    Convert a YES price into the favorite side and its contract cost.
    """
    if yes_price is None or pd.isna(yes_price):
        return None, None

    yes_price = max(0.0, min(1.0, float(yes_price)))
    no_price = 1.0 - yes_price
    if yes_price >= no_price:
        return "yes", yes_price
    return "no", no_price


def contract_pnl(favorite_price: float, won: bool, fee_rate: float) -> float:
    """
    Net P&L per one binary contract after approximating a fee on winnings only.

    If contract cost is p:
    - Win profit before fee: (1 - p)
    - Net win profit after fee: (1 - p) * (1 - fee_rate)
    - Loss P&L: -p
    """
    if won:
        return (1.0 - favorite_price) * (1.0 - fee_rate)
    return -favorite_price


def choose_observation(candle_df: pd.DataFrame, cutoff_time: pd.Timestamp) -> Tuple[Optional[pd.Timestamp], Optional[float]]:
    """
    Return the last available YES price at or before the exact cutoff time.
    """
    if candle_df.empty:
        return None, None

    eligible = candle_df[candle_df["end_period_ts"] <= cutoff_time]
    if eligible.empty:
        return None, None

    row = eligible.iloc[-1]
    return row["end_period_ts"], row["yes_price"]


def earliest_dry_up_time(candle_df: pd.DataFrame) -> Optional[pd.Timestamp]:
    """
    Return the earliest candle where one side appears effectively non-tradeable
    because the favorite side costs 99c+.
    """
    if candle_df.empty:
        return None

    favorite_prices = candle_df["yes_price"].apply(lambda x: favorite_from_yes_price(x)[1])
    dried = candle_df.loc[favorite_prices >= DRY_UP_PRICE]
    if dried.empty:
        return None
    return dried.iloc[0]["end_period_ts"]


def extract_market_close_time(market: Dict[str, Any]) -> Optional[pd.Timestamp]:
    """
    Close/expiry timestamps can appear under multiple adjacent fields.
    """
    for key in ["close_time", "expiration_time", "latest_expiration_time", "expected_expiration_time"]:
        ts = safe_timestamp(market.get(key))
        if ts is not None:
            return ts
    return None


def extract_market_result(market: Dict[str, Any]) -> Optional[str]:
    """
    Result field names can drift slightly across market/historical endpoints.
    """
    for key in ["result", "market_result", "settlement_result", "winning_outcome"]:
        result = market_result_normalized(market.get(key))
        if result is not None:
            return result
    return None


# ============================================================================
# Core analysis
# ============================================================================

def analyze_coin(
    session: requests.Session,
    *,
    series_ticker: str,
    coin_name: str,
    max_markets: int,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """
    Analyze one Kalshi 15-minute crypto market series.

    Returns:
        raw_df: one row per market x time window
        summary_df: aggregated performance by window x threshold
        metadata: higher-level coin-level stats for master ranking
    """
    markets = fetch_markets(session, series_ticker=series_ticker, max_markets=max_markets)
    if not markets:
        print(f"Warning: no markets returned for {coin_name} ({series_ticker}); skipping.", file=sys.stderr)
        return pd.DataFrame(), pd.DataFrame(), {"coin": coin_name, "series_ticker": series_ticker}

    observations: List[WindowObservation] = []
    skipped_markets = 0

    for market in tqdm(markets, desc=f"{coin_name}: markets", leave=False):
        market_ticker = market.get("ticker")
        close_time = extract_market_close_time(market)
        result = extract_market_result(market)
        status = str(market.get("status", "")).lower()

        if not market_ticker or close_time is None or result not in {"yes", "no"}:
            skipped_markets += 1
            continue

        if status in {"open", "active", "initialized", "inactive", "paused"}:
            # We want closed/settled historical observations only.
            skipped_markets += 1
            continue

        start_ts = int((close_time - timedelta(minutes=20)).timestamp())
        end_ts = int((close_time + timedelta(minutes=1)).timestamp())
        raw_candles = fetch_candlesticks(
            session,
            series_ticker=series_ticker,
            market_ticker=market_ticker,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        candle_df = normalize_candlesticks(raw_candles)

        if candle_df.empty:
            skipped_markets += 1
            continue

        dry_up_ts = earliest_dry_up_time(candle_df)

        for window_label, window_seconds in TIME_WINDOWS:
            cutoff_time = close_time - timedelta(seconds=window_seconds)
            observed_ts, yes_price = choose_observation(candle_df, cutoff_time)
            favorite_side, favorite_price = favorite_from_yes_price(yes_price)

            won = None
            winning_side = None
            pnl = None
            ev_per_dollar_risked = None
            tradeable = False
            dried_up_by_window = None

            if favorite_side is not None and favorite_price is not None:
                winning_side = favorite_side == result
                won = winning_side
                pnl = contract_pnl(favorite_price, won, FEE_RATE)
                if favorite_price > 0:
                    ev_per_dollar_risked = pnl / favorite_price
                tradeable = favorite_price < TRADEABLE_MAX_PRICE
                dried_up_by_window = favorite_price >= DRY_UP_PRICE
                if dry_up_ts is not None and dry_up_ts <= cutoff_time:
                    dried_up_by_window = True

            observations.append(
                WindowObservation(
                    coin=coin_name,
                    series_ticker=series_ticker,
                    market_ticker=market_ticker,
                    close_time=close_time,
                    result=result,
                    window_label=window_label,
                    window_seconds=window_seconds,
                    cutoff_time=cutoff_time,
                    observed_ts=observed_ts,
                    yes_price=yes_price,
                    favorite_side=favorite_side,
                    favorite_price=favorite_price,
                    winning_side=winning_side,
                    won=won,
                    pnl_per_contract=pnl,
                    ev_per_dollar_risked=ev_per_dollar_risked,
                    tradeable=tradeable,
                    dried_up_by_window=dried_up_by_window,
                )
            )

    raw_df = pd.DataFrame([vars(obs) for obs in observations])
    if raw_df.empty:
        print(f"Warning: {coin_name} produced no usable observations after filtering.", file=sys.stderr)
        return raw_df, pd.DataFrame(), {"coin": coin_name, "series_ticker": series_ticker}

    raw_df["favorite_price_cents"] = raw_df["favorite_price"] * 100.0
    raw_df["threshold_bucket_cents"] = raw_df["favorite_price_cents"].apply(assign_threshold_bucket)

    summary_df = aggregate_summary(raw_df, coin_name)
    metadata = build_coin_metadata(raw_df, summary_df, coin_name, series_ticker, len(markets), skipped_markets)
    return raw_df, summary_df, metadata


def assign_threshold_bucket(price_cents: Optional[float]) -> Optional[int]:
    """
    Map a favorite price into the highest qualifying threshold bucket.
    Example: 95.0c qualifies for 88, 90, 92, 94 but not 96.

    Aggregation later uses >= threshold semantics rather than exclusive bins.
    """
    if price_cents is None or pd.isna(price_cents):
        return None
    qualifying = [threshold for threshold in PRICE_THRESHOLDS_CENTS if price_cents >= threshold]
    return max(qualifying) if qualifying else None


def aggregate_summary(raw_df: pd.DataFrame, coin_name: str) -> pd.DataFrame:
    """
    Build thresholded summary statistics for one coin.
    """
    rows: List[Dict[str, Any]] = []

    valid = raw_df.dropna(subset=["favorite_price", "won"]).copy()
    if valid.empty:
        return pd.DataFrame()

    for window_label, window_seconds in TIME_WINDOWS:
        window_df = valid[valid["window_label"] == window_label].copy()
        if window_df.empty:
            continue

        for threshold in PRICE_THRESHOLDS_CENTS:
            subset = window_df[window_df["favorite_price_cents"] >= threshold].copy()
            if subset.empty:
                continue

            wins = int(subset["won"].sum())
            trades = int(len(subset))
            win_rate = wins / trades
            avg_entry_price = float(subset["favorite_price"].mean())
            avg_pnl = float(subset["pnl_per_contract"].mean())
            avg_ev_per_dollar_risked = float((subset["pnl_per_contract"] / subset["favorite_price"]).mean())
            tradeable_rate = float(subset["tradeable"].mean())
            dried_up_pct = float(subset["dried_up_by_window"].fillna(False).mean())

            bankroll_end = simulate_bankroll(
                initial_bankroll=STARTING_BANKROLL,
                risk_fraction=BANKROLL_RISK_FRACTION,
                win_rate=win_rate,
                avg_entry_price=avg_entry_price,
                fee_rate=FEE_RATE,
                n_trades=BANKROLL_SIM_TRADES,
            )

            rows.append(
                {
                    "coin": coin_name,
                    "window_label": window_label,
                    "window_seconds": window_seconds,
                    "threshold_cents": threshold,
                    "sample_size": trades,
                    "wins": wins,
                    "win_rate_pct": win_rate * 100.0,
                    "avg_entry_price_cents": avg_entry_price * 100.0,
                    "avg_profit_per_contract_cents": avg_pnl * 100.0,
                    "ev_per_trade_cents": avg_pnl * 100.0,
                    "ev_per_1_dollar_risked_cents": avg_ev_per_dollar_risked * 100.0,
                    "tradeable_rate_pct": tradeable_rate * 100.0,
                    "dry_up_rate_pct": dried_up_pct * 100.0,
                    "projected_bankroll_after_300": bankroll_end,
                }
            )

    summary_df = pd.DataFrame(rows)
    if summary_df.empty:
        return summary_df

    summary_df = summary_df.sort_values(
        by=["ev_per_1_dollar_risked_cents", "win_rate_pct", "sample_size"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    return summary_df


def build_coin_metadata(
    raw_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    coin_name: str,
    series_ticker: str,
    markets_fetched: int,
    skipped_markets: int,
) -> Dict[str, Any]:
    """
    Derive coin-level ranking metadata from detailed and aggregated results.
    """
    valid = raw_df.dropna(subset=["favorite_price", "won"]).copy()
    best_row = summary_df.iloc[0].to_dict() if not summary_df.empty else {}

    t60 = valid[valid["window_label"] == "T-60s"].copy()
    final_minute_96 = t60[t60["favorite_price"] >= UPSET_THRESHOLD]
    upset_rate = 100.0 * (1.0 - final_minute_96["won"].mean()) if not final_minute_96.empty else float("nan")

    per_window_tradeable = (
        valid.groupby("window_label")["tradeable"].mean().mul(100).to_dict() if not valid.empty else {}
    )
    per_window_dry_up = (
        valid.groupby("window_label")["dried_up_by_window"].mean().mul(100).to_dict() if not valid.empty else {}
    )

    tradeable_score = float(valid["tradeable"].mean() * 100.0) if not valid.empty else float("nan")
    longer_tradeable_flag = "YES" if tradeable_score >= 50.0 else "NO"

    return {
        "coin": coin_name,
        "series_ticker": series_ticker,
        "markets_fetched": markets_fetched,
        "markets_skipped": skipped_markets,
        "usable_observations": len(valid),
        "best_window": best_row.get("window_label"),
        "best_threshold_cents": best_row.get("threshold_cents"),
        "best_win_rate_pct": best_row.get("win_rate_pct"),
        "best_ev_per_1_dollar_risked_cents": best_row.get("ev_per_1_dollar_risked_cents"),
        "best_sample_size": best_row.get("sample_size"),
        "best_tradeable_rate_pct": best_row.get("tradeable_rate_pct"),
        "final_minute_upset_rate_pct_96plus": upset_rate,
        "avg_tradeable_rate_pct": tradeable_score,
        "stays_tradeable_longer": longer_tradeable_flag,
        "tradeable_rate_by_window": per_window_tradeable,
        "dry_up_rate_by_window": per_window_dry_up,
    }


# ============================================================================
# Bankroll simulation / reporting
# ============================================================================

def simulate_bankroll(
    *,
    initial_bankroll: float,
    risk_fraction: float,
    win_rate: float,
    avg_entry_price: float,
    fee_rate: float,
    n_trades: int,
) -> float:
    """
    Deterministic expected-growth bankroll path using flat fraction of current
    bankroll per trade and average edge assumptions.

    Position sizing:
    - Risk "stake_cash" = bankroll * risk_fraction per trade
    - Number of contracts bought = stake_cash / avg_entry_price
    """
    bankroll = float(initial_bankroll)
    if avg_entry_price <= 0 or n_trades <= 0:
        return bankroll

    win_multiplier = ((1.0 - avg_entry_price) * (1.0 - fee_rate)) / avg_entry_price
    loss_multiplier = -1.0

    expected_return_per_stake = win_rate * win_multiplier + (1.0 - win_rate) * loss_multiplier

    for _ in range(n_trades):
        bankroll += bankroll * risk_fraction * expected_return_per_stake
        if bankroll <= 0:
            return 0.0

    return bankroll


def best_entry_windows(summary_df: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    """
    Rank best entry windows for console display.
    """
    if summary_df.empty:
        return pd.DataFrame()

    ranked = summary_df.sort_values(
        by=["ev_per_1_dollar_risked_cents", "win_rate_pct", "tradeable_rate_pct", "sample_size"],
        ascending=[False, False, False, False],
    ).copy()
    cols = [
        "window_label",
        "threshold_cents",
        "sample_size",
        "win_rate_pct",
        "ev_per_1_dollar_risked_cents",
        "tradeable_rate_pct",
        "dry_up_rate_pct",
        "projected_bankroll_after_300",
    ]
    display = ranked.loc[:, cols].head(top_n).copy()
    for col in ["win_rate_pct", "ev_per_1_dollar_risked_cents", "tradeable_rate_pct", "dry_up_rate_pct"]:
        display[col] = display[col].map(lambda x: round(float(x), 2))
    display["projected_bankroll_after_300"] = display["projected_bankroll_after_300"].map(lambda x: round(float(x), 2))
    return display


def pick_recommendation_row(summary_df: pd.DataFrame, min_sample: int = 12) -> Optional[pd.Series]:
    """
    Pick the best actionable row with a light minimum sample filter.
    """
    if summary_df.empty:
        return None

    filtered = summary_df[summary_df["sample_size"] >= min_sample].copy()
    if filtered.empty:
        filtered = summary_df.copy()

    filtered = filtered.sort_values(
        by=["ev_per_1_dollar_risked_cents", "win_rate_pct", "tradeable_rate_pct", "sample_size"],
        ascending=[False, False, False, False],
    )
    return filtered.iloc[0]


def save_outputs(raw_df: pd.DataFrame, summary_df: pd.DataFrame, coin_name: str) -> None:
    """
    Save per-coin CSV outputs.
    """
    raw_path = f"{coin_name}_15min_raw_data.csv"
    summary_path = f"{coin_name}_summary_stats.csv"
    raw_df.to_csv(raw_path, index=False)
    summary_df.to_csv(summary_path, index=False)


def generate_chart(all_summaries: pd.DataFrame) -> Optional[str]:
    """
    Save a simple bar chart for BTC/ETH win rates across time windows at the
    chosen threshold.
    """
    if plt is None or sns is None:
        return None

    if all_summaries.empty:
        return None

    plot_df = all_summaries[
        (all_summaries["coin"].isin(CHART_COINS))
        & (all_summaries["threshold_cents"] == CHART_THRESHOLD_CENTS)
    ].copy()
    if plot_df.empty:
        return None

    ordered_windows = [label for label, _ in TIME_WINDOWS]
    plot_df["window_label"] = pd.Categorical(plot_df["window_label"], categories=ordered_windows, ordered=True)
    plot_df = plot_df.sort_values(["coin", "window_label"])

    sns.set_theme(style="whitegrid")
    plt.figure(figsize=(10, 5))
    ax = sns.barplot(
        data=plot_df,
        x="window_label",
        y="win_rate_pct",
        hue="coin",
        palette="deep",
    )
    ax.set_title(f"Kalshi 15m Crypto: Win Rate by Window at {CHART_THRESHOLD_CENTS}c+ Favorite Price")
    ax.set_xlabel("Pre-expiry window")
    ax.set_ylabel("Win rate (%)")
    ax.legend(title="Coin")
    plt.tight_layout()
    chart_path = "BTC_ETH_winrate_by_window.png"
    plt.savefig(chart_path, dpi=180)
    plt.close()
    return chart_path


def print_coin_report(coin_name: str, summary_df: pd.DataFrame, metadata: Dict[str, Any]) -> None:
    """
    Print clean Markdown output for one coin.
    """
    print(f"\n# {coin_name} 15-Minute Expiry Scalping")
    print("")
    print("## Best entry windows")
    print("")
    print(markdown_table(best_entry_windows(summary_df, top_n=5)))
    print("")

    rec = pick_recommendation_row(summary_df)
    if rec is not None:
        print("## Summary")
        print("")
        print(
            f"- At {rec['window_label']} / {int(rec['threshold_cents'])}c+ on {coin_name}: "
            f"{rec['win_rate_pct']:.2f}% win rate -> "
            f"{rec['ev_per_1_dollar_risked_cents']:.2f}c EV per $1 risked "
            f"across {int(rec['sample_size'])} trades."
        )
        print(
            f"- Tradeable rate: {rec['tradeable_rate_pct']:.2f}% | "
            f"Dry-up rate: {rec['dry_up_rate_pct']:.2f}% | "
            f"Projected bankroll after 300 trades: ${rec['projected_bankroll_after_300']:.2f}"
        )
    else:
        print("## Summary")
        print("")
        print("- No usable threshold summary rows were produced.")

    upset = metadata.get("final_minute_upset_rate_pct_96plus")
    tradeable_longer = metadata.get("stays_tradeable_longer", "NO")
    if upset == upset:
        print(f"- Final-minute upset rate for 96c+ favorites at T-60s: {upset:.2f}%")
    print(f"- Stays tradeable longer: {tradeable_longer}")


def build_master_ranking(metadata_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    Build final cross-coin recommendation ranking.
    """
    ranking = pd.DataFrame(metadata_rows)
    if ranking.empty:
        return ranking

    ranking["rank_score"] = (
        ranking["best_ev_per_1_dollar_risked_cents"].fillna(-999)
        + 0.20 * ranking["best_win_rate_pct"].fillna(0)
        + 0.05 * ranking["avg_tradeable_rate_pct"].fillna(0)
        - 0.05 * ranking["final_minute_upset_rate_pct_96plus"].fillna(0)
    )

    ranking = ranking.sort_values(
        by=["rank_score", "best_ev_per_1_dollar_risked_cents", "best_win_rate_pct", "avg_tradeable_rate_pct"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    ranking.insert(0, "overall_rank", range(1, len(ranking) + 1))
    return ranking


def print_final_recommendation(ranking_df: pd.DataFrame) -> None:
    """
    Print a final strategy recommendation box.
    """
    print("\n# Strategy Recommendation\n")
    if ranking_df.empty:
        print("No ranking could be produced.")
        return

    top = ranking_df.iloc[0]
    print("+--------------------------------------------------------------+")
    print("|                      STRATEGY RECOMMENDATION                 |")
    print("+--------------------------------------------------------------+")
    print(
        f"| Best coin: {str(top['coin']).ljust(49)}|"
    )
    print(
        f"| Best setup: {f'{top['best_window']} / {int(top['best_threshold_cents'])}c+'.ljust(46)}|"
    )
    print(
        f"| EV per $1 risked: {f'{top['best_ev_per_1_dollar_risked_cents']:.2f}c'.ljust(37)}|"
    )
    print(
        f"| Win rate: {f'{top['best_win_rate_pct']:.2f}%'.ljust(44)}|"
    )
    print(
        f"| Tradeable longer: {str(top['stays_tradeable_longer']).ljust(38)}|"
    )
    print("+--------------------------------------------------------------+")


# ============================================================================
# Main entry point
# ============================================================================

def main() -> None:
    """
    One-click runner over all configured crypto 15-minute series.
    """
    warnings.filterwarnings("ignore", category=FutureWarning)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 50)

    session = build_session()

    all_raw: List[pd.DataFrame] = []
    all_summaries: List[pd.DataFrame] = []
    metadata_rows: List[Dict[str, Any]] = []

    print("# Kalshi 15-Minute Crypto Expiry Scalper Analysis")
    print("")
    print(f"- Run timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"- Fee rate on winnings: {FEE_RATE:.2%}")
    print(f"- Max markets per coin: {MAX_MARKETS_PER_COIN}")
    print(f"- Thresholds: {', '.join(str(x) for x in PRICE_THRESHOLDS_CENTS)} cents")

    for coin_name, series_ticker in tqdm(COIN_SERIES.items(), desc="Coins"):
        raw_df, summary_df, metadata = analyze_coin(
            session,
            series_ticker=series_ticker,
            coin_name=coin_name,
            max_markets=MAX_MARKETS_PER_COIN,
        )
        metadata_rows.append(metadata)

        if raw_df.empty or summary_df.empty:
            print(f"\nWarning: {coin_name} skipped due to insufficient usable data.\n", file=sys.stderr)
            continue

        save_outputs(raw_df, summary_df, coin_name)
        print_coin_report(coin_name, summary_df, metadata)

        all_raw.append(raw_df)
        all_summaries.append(summary_df)

    if not all_summaries:
        print("\nNo summaries were produced. Check network access, endpoint availability, or ticker validity.", file=sys.stderr)
        return

    all_raw_df = pd.concat(all_raw, ignore_index=True)
    all_summary_df = pd.concat(all_summaries, ignore_index=True)
    ranking_df = build_master_ranking(metadata_rows)

    all_raw_df.to_csv("ALL_COINS_RAW_DATA.csv", index=False)
    all_summary_df.to_csv("ALL_COINS_SUMMARY.csv", index=False)
    ranking_df.to_csv("ALL_COINS_RANKING.csv", index=False)

    chart_path = generate_chart(all_summary_df)
    if chart_path:
        print(f"\nSaved chart: {chart_path}")
    elif plt is None or sns is None:
        print("\nSkipped chart generation because matplotlib/seaborn are not installed.")

    print("\n# Overall Ranking\n")
    display_cols = [
        "overall_rank",
        "coin",
        "best_window",
        "best_threshold_cents",
        "best_win_rate_pct",
        "best_ev_per_1_dollar_risked_cents",
        "avg_tradeable_rate_pct",
        "final_minute_upset_rate_pct_96plus",
        "stays_tradeable_longer",
    ]
    ranking_display = ranking_df[display_cols].copy()
    for col in [
        "best_win_rate_pct",
        "best_ev_per_1_dollar_risked_cents",
        "avg_tradeable_rate_pct",
        "final_minute_upset_rate_pct_96plus",
    ]:
        ranking_display[col] = ranking_display[col].map(
            lambda x: round(float(x), 2) if pd.notna(x) else None
        )
    print(markdown_table(ranking_display))

    print_final_recommendation(ranking_df)


if __name__ == "__main__":
    main()
