#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry


KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
DEFAULT_SERIES_TICKER = "KXBTC15M"
DEFAULT_MAX_MARKETS = 240
DEFAULT_FEE_RATE = 0.07
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_SLEEP_SECONDS = 0.08
PER_PAGE_LIMIT = 200
COINBASE_MAX_CANDLES_PER_REQUEST = 300

TIME_WINDOWS: List[Tuple[str, int]] = [
    ("T-5m", 300),
    ("T-4m", 240),
    ("T-3m", 180),
    ("T-2m", 120),
    ("T-90s", 90),
    ("T-60s", 60),
]
WINDOW_SECONDS = dict(TIME_WINDOWS)

PRICE_THRESHOLDS = [0.60, 0.70, 0.80, 0.90, 0.92, 0.94]
MAX_SPREADS = [0.03, 0.05, 0.08]
MOMENTUM_LOOKBACKS = [1, 3, 5]
MOMENTUM_MAGNITUDES_BPS = [0, 4, 8]
RULE_ACTIONS = ["follow_favorite", "fade_favorite"]
MOMENTUM_FILTERS = ["ignore", "confirm", "contradict"]


@dataclass(frozen=True)
class Rule:
    action: str
    window_label: str
    min_favorite_price: float
    max_spread: float
    lookback_minutes: int
    momentum_filter: str
    min_abs_return_bps: int

    def label(self) -> str:
        return (
            f"{self.action} | {self.window_label} | favorite>={self.min_favorite_price:.2f} | "
            f"spread<={self.max_spread:.2f} | mom{self.lookback_minutes}m={self.momentum_filter} | "
            f"|ret|>={self.min_abs_return_bps}bps"
        )


def build_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "btc-kalshi-analysis/2.0",
        }
    )
    return session


def safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_timestamp(value: Any) -> Optional[pd.Timestamp]:
    if value is None or value == "":
        return None
    try:
        return pd.to_datetime(value, utc=True)
    except Exception:
        return None


def request_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    context: str,
) -> Dict[str, Any]:
    response = session.get(
        url,
        params=params,
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    time.sleep(REQUEST_SLEEP_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{context}: expected JSON object response")
    return payload


def fetch_settled_markets(
    session: requests.Session,
    *,
    series_ticker: str,
    max_markets: int,
) -> List[Dict[str, Any]]:
    markets: List[Dict[str, Any]] = []
    cursor: Optional[str] = None

    while len(markets) < max_markets:
        params: Dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": "settled",
            "limit": min(PER_PAGE_LIMIT, max_markets - len(markets)),
        }
        if cursor:
            params["cursor"] = cursor

        payload = request_json(
            session,
            f"{KALSHI_BASE_URL}/markets",
            params=params,
            context="fetch_settled_markets",
        )
        batch = payload.get("markets") or []
        if not isinstance(batch, list):
            break
        markets.extend(batch)
        cursor = payload.get("cursor")
        if not cursor:
            break

    deduped: Dict[str, Dict[str, Any]] = {}
    for market in markets:
        ticker = market.get("ticker")
        if ticker:
            deduped[ticker] = market

    output = list(deduped.values())
    output.sort(key=lambda row: safe_timestamp(row.get("close_time")) or pd.Timestamp.min.tz_localize("UTC"))
    return output[:max_markets]


def fetch_market_candles(
    session: requests.Session,
    *,
    series_ticker: str,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
) -> pd.DataFrame:
    payload = request_json(
        session,
        f"{KALSHI_BASE_URL}/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        params={
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": 1,
            "include_latest_before_start": "true",
        },
        context=f"fetch_market_candles:{market_ticker}",
    )
    candles = payload.get("candlesticks") or []
    if not candles:
        return pd.DataFrame(
            columns=["end_period_ts", "yes_bid", "yes_ask", "price_close", "volume_fp", "open_interest_fp"]
        )

    rows: List[Dict[str, Any]] = []
    for candle in candles:
        price = candle.get("price") or {}
        yes_bid = candle.get("yes_bid") or {}
        yes_ask = candle.get("yes_ask") or {}
        rows.append(
            {
                "end_period_ts": pd.to_datetime(int(candle["end_period_ts"]), unit="s", utc=True),
                "yes_bid": safe_float(yes_bid.get("close_dollars")),
                "yes_ask": safe_float(yes_ask.get("close_dollars")),
                "price_close": safe_float(price.get("close_dollars")),
                "volume_fp": safe_float(candle.get("volume_fp")),
                "open_interest_fp": safe_float(candle.get("open_interest_fp")),
            }
        )

    df = pd.DataFrame(rows).sort_values("end_period_ts").reset_index(drop=True)
    return df


def fetch_coinbase_candles(
    session: requests.Session,
    *,
    start_time: pd.Timestamp,
    end_time: pd.Timestamp,
) -> pd.DataFrame:
    all_rows: List[Dict[str, Any]] = []
    chunk_start = start_time.floor("min")
    end_time = end_time.ceil("min")

    while chunk_start < end_time:
        chunk_end = min(
            chunk_start + timedelta(minutes=COINBASE_MAX_CANDLES_PER_REQUEST - 1),
            end_time,
        )
        payload = session.get(
            COINBASE_CANDLES_URL,
            params={
                "granularity": 60,
                "start": chunk_start.isoformat(),
                "end": chunk_end.isoformat(),
            },
            headers={"User-Agent": "btc-kalshi-analysis/2.0"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        time.sleep(REQUEST_SLEEP_SECONDS)
        payload.raise_for_status()
        rows = payload.json()
        if not isinstance(rows, list):
            raise ValueError("fetch_coinbase_candles: unexpected response type")

        for item in rows:
            if not isinstance(item, list) or len(item) < 6:
                continue
            all_rows.append(
                {
                    "ts": pd.to_datetime(int(item[0]), unit="s", utc=True),
                    "low": float(item[1]),
                    "high": float(item[2]),
                    "open": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
            )
        chunk_start = chunk_end + timedelta(minutes=1)

    btc_df = pd.DataFrame(all_rows).drop_duplicates("ts")
    if btc_df.empty:
        raise RuntimeError("Coinbase returned no BTC candles for the requested range.")
    return btc_df.sort_values("ts").reset_index(drop=True)


def choose_snapshot(candle_df: pd.DataFrame, cutoff_time: pd.Timestamp) -> Optional[pd.Series]:
    if candle_df.empty:
        return None
    eligible = candle_df[candle_df["end_period_ts"] <= cutoff_time]
    if eligible.empty:
        return None
    return eligible.iloc[-1]


def derive_favorite_side(yes_bid: Optional[float], yes_ask: Optional[float]) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    if yes_bid is None and yes_ask is None:
        return None, None, None

    yes_mid = None
    if yes_bid is not None and yes_ask is not None:
        yes_mid = (yes_bid + yes_ask) / 2.0
    elif yes_ask is not None:
        yes_mid = yes_ask
    elif yes_bid is not None:
        yes_mid = yes_bid

    if yes_mid is None:
        return None, None, None

    favorite_side = "yes" if yes_mid >= 0.5 else "no"
    favorite_entry_price = yes_ask if favorite_side == "yes" else (1.0 - yes_bid if yes_bid is not None else None)
    spread = None
    if yes_bid is not None and yes_ask is not None:
        spread = yes_ask - yes_bid
    return favorite_side, favorite_entry_price, spread


def contract_pnl(entry_price: float, won: bool, fee_rate: float) -> float:
    return (1.0 - entry_price) * (1.0 - fee_rate) if won else -entry_price


def get_btc_price_before_or_at(btc_df: pd.DataFrame, ts_value: pd.Timestamp) -> Optional[pd.Series]:
    eligible = btc_df[btc_df["ts"] <= ts_value]
    if eligible.empty:
        return None
    return eligible.iloc[-1]


def compute_btc_return_bps(
    btc_df: pd.DataFrame,
    *,
    ts_value: pd.Timestamp,
    lookback_minutes: int,
) -> Optional[float]:
    current = get_btc_price_before_or_at(btc_df, ts_value)
    if current is None:
        return None
    previous = get_btc_price_before_or_at(btc_df, ts_value - timedelta(minutes=lookback_minutes))
    if previous is None or previous["close"] <= 0:
        return None
    return ((current["close"] / previous["close"]) - 1.0) * 10000.0


def build_observations(
    session: requests.Session,
    *,
    series_ticker: str,
    markets: Sequence[Dict[str, Any]],
    fee_rate: float,
) -> pd.DataFrame:
    market_rows: List[Dict[str, Any]] = []
    close_times: List[pd.Timestamp] = []
    for market in markets:
        close_time = safe_timestamp(market.get("close_time"))
        result = str(market.get("result", "")).lower()
        ticker = market.get("ticker")
        if close_time is None or ticker is None or result not in {"yes", "no"}:
            continue
        market_rows.append(market)
        close_times.append(close_time)

    if not market_rows:
        return pd.DataFrame()

    btc_df = fetch_coinbase_candles(
        session,
        start_time=min(close_times) - timedelta(minutes=30),
        end_time=max(close_times) + timedelta(minutes=5),
    )

    observations: List[Dict[str, Any]] = []
    failures = 0

    for market in market_rows:
        ticker = str(market["ticker"])
        close_time = safe_timestamp(market["close_time"])
        open_time = safe_timestamp(market.get("open_time"))
        result = str(market["result"]).lower()
        if close_time is None:
            failures += 1
            continue

        start_ts = int((close_time - timedelta(minutes=20)).timestamp())
        end_ts = int((close_time + timedelta(minutes=1)).timestamp())

        try:
            candle_df = fetch_market_candles(
                session,
                series_ticker=series_ticker,
                market_ticker=ticker,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception as exc:
            print(f"Warning: failed to fetch candlesticks for {ticker}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if candle_df.empty:
            failures += 1
            continue

        for window_label, seconds_before_close in TIME_WINDOWS:
            cutoff_time = close_time - timedelta(seconds=seconds_before_close)
            snapshot = choose_snapshot(candle_df, cutoff_time)
            if snapshot is None:
                continue

            yes_bid = snapshot["yes_bid"]
            yes_ask = snapshot["yes_ask"]
            favorite_side, favorite_entry_price, spread = derive_favorite_side(yes_bid, yes_ask)
            if favorite_side is None or favorite_entry_price is None:
                continue

            btc_now = get_btc_price_before_or_at(btc_df, cutoff_time)
            if btc_now is None:
                continue

            row = {
                "ticker": ticker,
                "open_time": open_time,
                "close_time": close_time,
                "result": result,
                "window_label": window_label,
                "window_seconds": seconds_before_close,
                "cutoff_time": cutoff_time,
                "snapshot_ts": snapshot["end_period_ts"],
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "favorite_side": favorite_side,
                "favorite_entry_price": favorite_entry_price,
                "favorite_price_cents": favorite_entry_price * 100.0,
                "spread": spread,
                "spread_cents": spread * 100.0 if spread is not None else None,
                "favorite_won": favorite_side == result,
                "favorite_pnl": contract_pnl(favorite_entry_price, favorite_side == result, fee_rate),
                "btc_price": btc_now["close"],
                "btc_ret_1m_bps": compute_btc_return_bps(btc_df, ts_value=cutoff_time, lookback_minutes=1),
                "btc_ret_3m_bps": compute_btc_return_bps(btc_df, ts_value=cutoff_time, lookback_minutes=3),
                "btc_ret_5m_bps": compute_btc_return_bps(btc_df, ts_value=cutoff_time, lookback_minutes=5),
                "kalshi_volume_fp": snapshot["volume_fp"],
                "kalshi_open_interest_fp": snapshot["open_interest_fp"],
            }
            for lookback_minutes in MOMENTUM_LOOKBACKS:
                momentum_value = row[f"btc_ret_{lookback_minutes}m_bps"]
                row[f"favorite_matches_btc_{lookback_minutes}m"] = (
                    None
                    if momentum_value is None or pd.isna(momentum_value) or momentum_value == 0
                    else (
                        (momentum_value > 0 and favorite_side == "yes")
                        or (momentum_value < 0 and favorite_side == "no")
                    )
                )
            observations.append(row)

    if not observations:
        raise RuntimeError("No usable BTC/Kalshi observations were built.")

    df = pd.DataFrame(observations).sort_values(["close_time", "window_seconds"]).reset_index(drop=True)
    df["market_sequence"] = df["ticker"].astype("category").cat.codes
    print(f"- Built {len(df)} observations from {df['ticker'].nunique()} settled markets ({failures} markets skipped).")
    return df


def opposite_side(side: str) -> str:
    return "no" if side == "yes" else "yes"


def trade_entry_price(row: pd.Series, side: str) -> Optional[float]:
    if side == "yes":
        return row["yes_ask"]
    if row["yes_bid"] is None or pd.isna(row["yes_bid"]):
        return None
    return 1.0 - float(row["yes_bid"])


def strategy_side(row: pd.Series, action: str) -> str:
    return row["favorite_side"] if action == "follow_favorite" else opposite_side(row["favorite_side"])


def apply_rule(df: pd.DataFrame, rule: Rule, fee_rate: float) -> pd.DataFrame:
    subset = df[df["window_label"] == rule.window_label].copy()
    if subset.empty:
        return subset

    subset = subset[subset["favorite_entry_price"] >= rule.min_favorite_price]
    subset = subset[subset["spread"].notna() & (subset["spread"] <= rule.max_spread)]

    momentum_col = f"btc_ret_{rule.lookback_minutes}m_bps"
    subset = subset[subset[momentum_col].notna()]
    subset = subset[subset[momentum_col].abs() >= rule.min_abs_return_bps]

    if rule.momentum_filter != "ignore":
        matches = subset[f"favorite_matches_btc_{rule.lookback_minutes}m"].fillna(False).astype(bool)
        subset = subset.loc[matches] if rule.momentum_filter == "confirm" else subset.loc[~matches]

    if subset.empty:
        return subset

    subset = subset.copy()
    follow_mask = subset["favorite_side"] == "yes"
    if rule.action == "follow_favorite":
        subset["trade_side"] = subset["favorite_side"]
        subset["entry_price"] = subset["favorite_entry_price"]
    else:
        subset["trade_side"] = subset["favorite_side"].map(opposite_side)
        subset["entry_price"] = subset["yes_ask"]
        subset.loc[follow_mask, "entry_price"] = 1.0 - subset.loc[follow_mask, "yes_bid"]

    subset = subset[subset["entry_price"].notna()]
    subset["won"] = subset["trade_side"].eq(subset["result"])
    subset["pnl"] = subset.apply(
        lambda row: contract_pnl(float(row["entry_price"]), bool(row["won"]), fee_rate),
        axis=1,
    )
    return subset


def confidence_interval_mean(values: Sequence[float]) -> Tuple[float, float]:
    n = len(values)
    if n == 0:
        return float("nan"), float("nan")
    mean = sum(values) / n
    if n == 1:
        return mean, mean
    variance = sum((value - mean) ** 2 for value in values) / (n - 1)
    stderr = math.sqrt(variance) / math.sqrt(n)
    half_width = 1.96 * stderr
    return mean - half_width, mean + half_width


def wilson_lower_bound(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0:
        return float("nan")
    phat = successes / trials
    denom = 1.0 + z * z / trials
    center = phat + z * z / (2.0 * trials)
    adjusted = z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * trials)) / trials)
    return (center - adjusted) / denom


def summarize_rule(trades: pd.DataFrame) -> Dict[str, Any]:
    sample_size = int(len(trades))
    wins = int(trades["won"].sum()) if sample_size else 0
    mean_pnl = float(trades["pnl"].mean()) if sample_size else float("nan")
    ci_low, ci_high = confidence_interval_mean(trades["pnl"].tolist()) if sample_size else (float("nan"), float("nan"))
    return {
        "sample_size": sample_size,
        "wins": wins,
        "win_rate": wins / sample_size if sample_size else float("nan"),
        "mean_pnl": mean_pnl,
        "mean_pnl_ci_low": ci_low,
        "mean_pnl_ci_high": ci_high,
        "avg_entry_price": float(trades["entry_price"].mean()) if sample_size else float("nan"),
        "wilson_win_rate_lower": wilson_lower_bound(wins, sample_size) if sample_size else float("nan"),
    }


def split_train_test(df: pd.DataFrame, train_fraction: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    market_order = df[["ticker", "close_time"]].drop_duplicates().sort_values("close_time").reset_index(drop=True)
    split_index = max(1, min(len(market_order) - 1, int(len(market_order) * train_fraction)))
    train_tickers = set(market_order.iloc[:split_index]["ticker"])
    test_tickers = set(market_order.iloc[split_index:]["ticker"])
    train_df = df[df["ticker"].isin(train_tickers)].copy()
    test_df = df[df["ticker"].isin(test_tickers)].copy()
    return train_df, test_df


def generate_rules() -> Iterable[Rule]:
    for action in RULE_ACTIONS:
        for window_label, _ in TIME_WINDOWS:
            for min_favorite_price in PRICE_THRESHOLDS:
                for max_spread in MAX_SPREADS:
                    for lookback_minutes in MOMENTUM_LOOKBACKS:
                        for momentum_filter in MOMENTUM_FILTERS:
                            for min_abs_return_bps in MOMENTUM_MAGNITUDES_BPS:
                                yield Rule(
                                    action=action,
                                    window_label=window_label,
                                    min_favorite_price=min_favorite_price,
                                    max_spread=max_spread,
                                    lookback_minutes=lookback_minutes,
                                    momentum_filter=momentum_filter,
                                    min_abs_return_bps=min_abs_return_bps,
                                )


def discover_rules(
    observations: pd.DataFrame,
    *,
    fee_rate: float,
    train_fraction: float,
    min_train_trades: int,
    min_test_trades: int,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    train_df, test_df = split_train_test(observations, train_fraction=train_fraction)
    rows: List[Dict[str, Any]] = []

    for rule in generate_rules():
        train_trades = apply_rule(train_df, rule, fee_rate)
        if len(train_trades) < min_train_trades:
            continue

        test_trades = apply_rule(test_df, rule, fee_rate)
        if len(test_trades) < min_test_trades:
            continue

        train_stats = summarize_rule(train_trades)
        test_stats = summarize_rule(test_trades)

        rows.append(
            {
                "rule": rule.label(),
                "action": rule.action,
                "window_label": rule.window_label,
                "min_favorite_price_cents": round(rule.min_favorite_price * 100.0, 1),
                "max_spread_cents": round(rule.max_spread * 100.0, 1),
                "lookback_minutes": rule.lookback_minutes,
                "momentum_filter": rule.momentum_filter,
                "min_abs_return_bps": rule.min_abs_return_bps,
                "train_sample_size": train_stats["sample_size"],
                "train_win_rate_pct": train_stats["win_rate"] * 100.0,
                "train_mean_pnl_cents": train_stats["mean_pnl"] * 100.0,
                "train_mean_pnl_ci_low_cents": train_stats["mean_pnl_ci_low"] * 100.0,
                "train_mean_pnl_ci_high_cents": train_stats["mean_pnl_ci_high"] * 100.0,
                "train_wilson_lower_pct": train_stats["wilson_win_rate_lower"] * 100.0,
                "test_sample_size": test_stats["sample_size"],
                "test_win_rate_pct": test_stats["win_rate"] * 100.0,
                "test_mean_pnl_cents": test_stats["mean_pnl"] * 100.0,
                "test_mean_pnl_ci_low_cents": test_stats["mean_pnl_ci_low"] * 100.0,
                "test_mean_pnl_ci_high_cents": test_stats["mean_pnl_ci_high"] * 100.0,
                "test_wilson_lower_pct": test_stats["wilson_win_rate_lower"] * 100.0,
                "test_avg_entry_price_cents": test_stats["avg_entry_price"] * 100.0,
                "train_score": min(
                    train_stats["mean_pnl"],
                    train_stats["mean_pnl_ci_low"],
                ),
                "test_score": min(
                    test_stats["mean_pnl"],
                    test_stats["mean_pnl_ci_low"],
                ),
            }
        )

    rules_df = pd.DataFrame(rows)
    if rules_df.empty:
        raise RuntimeError("No candidate rules met the train/test minimum trade counts.")

    rules_df = rules_df.sort_values(
        ["test_score", "test_mean_pnl_cents", "test_win_rate_pct", "test_sample_size"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    return train_df, test_df, rules_df


def baseline_summary(
    observations: pd.DataFrame,
    *,
    fee_rate: float,
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for window_label, _ in TIME_WINDOWS:
        for threshold in PRICE_THRESHOLDS:
            rule = Rule(
                action="follow_favorite",
                window_label=window_label,
                min_favorite_price=threshold,
                max_spread=0.08,
                lookback_minutes=1,
                momentum_filter="ignore",
                min_abs_return_bps=0,
            )
            trades = apply_rule(observations, rule, fee_rate)
            if len(trades) < 10:
                continue
            stats = summarize_rule(trades)
            rows.append(
                {
                    "window_label": window_label,
                    "favorite_price_threshold_cents": round(threshold * 100.0, 1),
                    "sample_size": stats["sample_size"],
                    "win_rate_pct": stats["win_rate"] * 100.0,
                    "mean_pnl_cents": stats["mean_pnl"] * 100.0,
                    "mean_pnl_ci_low_cents": stats["mean_pnl_ci_low"] * 100.0,
                    "mean_pnl_ci_high_cents": stats["mean_pnl_ci_high"] * 100.0,
                    "avg_entry_price_cents": stats["avg_entry_price"] * 100.0,
                }
            )
    baseline_df = pd.DataFrame(rows)
    if baseline_df.empty:
        return baseline_df
    return baseline_df.sort_values(
        ["mean_pnl_cents", "win_rate_pct", "sample_size"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def markdown_table(df: pd.DataFrame, max_rows: int = 10) -> str:
    if df.empty:
        return "_No rows_"
    display_df = df.head(max_rows).copy()
    display_df.columns = [str(col) for col in display_df.columns]
    for col in display_df.columns:
        display_df[col] = display_df[col].map(
            lambda value: "" if pd.isna(value) else str(value)
        )
    headers = list(display_df.columns)
    rows = display_df.values.tolist()
    widths = [
        max(len(headers[index]), *(len(str(row[index])) for row in rows))
        for index in range(len(headers))
    ]
    def fmt_row(values: Sequence[Any]) -> str:
        return "| " + " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(values)) + " |"
    separator = "| " + " | ".join("-" * width for width in widths) + " |"
    lines = [fmt_row(headers), separator]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def print_header(args: argparse.Namespace) -> None:
    print("# BTC 15-Minute Kalshi Market Analysis")
    print("")
    print(f"- Run timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"- Series ticker: {args.series}")
    print(f"- Max settled markets requested: {args.max_markets}")
    print(f"- Fee rate on winnings: {args.fee_rate:.2%}")
    print(f"- Train/test split: {args.train_fraction:.0%} / {(1.0 - args.train_fraction):.0%}")
    print(f"- Coinbase product: BTC-USD, 1-minute candles")
    print("- Entry prices are conservative executable estimates:")
    print("  - buy YES at candle close `yes_ask`")
    print("  - buy NO at `1 - yes_bid`")


def print_outputs(
    observations: pd.DataFrame,
    baselines: pd.DataFrame,
    rules_df: pd.DataFrame,
    *,
    train_fraction: float,
) -> None:
    date_min = observations["close_time"].min()
    date_max = observations["close_time"].max()
    market_count = observations["ticker"].nunique()

    print("")
    print("## Coverage")
    print("")
    print(f"- Markets analyzed: {market_count}")
    print(f"- Observation rows: {len(observations)}")
    print(f"- Date range: {date_min} -> {date_max}")
    print("")
    print("## Baseline Favorite Strategy")
    print("")
    if baselines.empty:
        print("_No baseline rows met the minimum sample size._")
    else:
        baseline_display = baselines[
            [
                "window_label",
                "favorite_price_threshold_cents",
                "sample_size",
                "win_rate_pct",
                "mean_pnl_cents",
                "mean_pnl_ci_low_cents",
                "avg_entry_price_cents",
            ]
        ].copy()
        for col in ["win_rate_pct", "mean_pnl_cents", "mean_pnl_ci_low_cents", "avg_entry_price_cents"]:
            baseline_display[col] = baseline_display[col].map(lambda x: round(float(x), 3))
        print(markdown_table(baseline_display, max_rows=12))
    print("")
    print("## Candidate Rules")
    print("")
    if {"rule", "train_sample_size", "train_mean_pnl_cents", "test_sample_size", "test_mean_pnl_cents", "test_mean_pnl_ci_low_cents", "test_win_rate_pct"}.issubset(rules_df.columns):
        rule_display = rules_df[
            [
                "rule",
                "train_sample_size",
                "train_mean_pnl_cents",
                "test_sample_size",
                "test_mean_pnl_cents",
                "test_mean_pnl_ci_low_cents",
                "test_win_rate_pct",
            ]
        ].copy()
        for col in [
            "train_mean_pnl_cents",
            "test_mean_pnl_cents",
            "test_mean_pnl_ci_low_cents",
            "test_win_rate_pct",
        ]:
            rule_display[col] = rule_display[col].map(lambda x: round(float(x), 3))
        print(markdown_table(rule_display, max_rows=12))
    else:
        print(markdown_table(rules_df, max_rows=12))
    print("")
    print("## Recommendation")
    print("")
    best = rules_df.iloc[0]
    if "test_mean_pnl_ci_low_cents" in best and float(best["test_mean_pnl_ci_low_cents"]) > 0:
        print(
            f"- Candidate edge survived the {train_fraction:.0%}/{(1.0 - train_fraction):.0%} split: "
            f"`{best['rule']}`"
        )
        print(
            f"- Out-of-sample mean PnL: {best['test_mean_pnl_cents']:.3f}c per contract "
            f"(95% CI low: {best['test_mean_pnl_ci_low_cents']:.3f}c) across "
            f"{int(best['test_sample_size'])} test trades."
        )
    else:
        print("- No rule cleared a positive out-of-sample lower confidence bound.")
        print("- Treat this run as signal research, not proof of a tradeable edge.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Research BTC 15-minute Kalshi markets using settled Kalshi candles "
            "plus Coinbase 1-minute BTC candles, then search simple out-of-sample rules."
        )
    )
    parser.add_argument("--series", default=DEFAULT_SERIES_TICKER)
    parser.add_argument("--max-markets", type=int, default=DEFAULT_MAX_MARKETS)
    parser.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--min-train-trades", type=int, default=25)
    parser.add_argument("--min-test-trades", type=int, default=10)
    parser.add_argument("--save-prefix", default="btc_kalshi")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pd.set_option("display.width", 160)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    session = build_session()

    print_header(args)
    try:
        markets = fetch_settled_markets(
            session,
            series_ticker=args.series,
            max_markets=args.max_markets,
        )
    except RequestException as exc:
        raise RuntimeError(
            "Network error while reaching Kalshi. Check DNS/internet access, then rerun. "
            "If you want live progress output, use `python -u analysis.py`."
        ) from exc
    if not markets:
        raise RuntimeError("No settled Kalshi BTC markets were returned.")

    try:
        observations = build_observations(
            session,
            series_ticker=args.series,
            markets=markets,
            fee_rate=args.fee_rate,
        )
    except RequestException as exc:
        raise RuntimeError(
            "Network error while fetching Kalshi or Coinbase data. Check internet access, then rerun. "
            "If you want live progress output, use `python -u analysis.py`."
        ) from exc
    baselines = baseline_summary(observations, fee_rate=args.fee_rate)
    try:
        _, _, rules_df = discover_rules(
            observations,
            fee_rate=args.fee_rate,
            train_fraction=args.train_fraction,
            min_train_trades=args.min_train_trades,
            min_test_trades=args.min_test_trades,
        )
    except RuntimeError as exc:
        rules_df = pd.DataFrame(
            [{"rule": f"No candidate rule: {exc}", "test_mean_pnl_ci_low_cents": float("-inf"), "test_sample_size": 0}]
        )

    observations.to_csv(f"{args.save_prefix}_observations.csv", index=False)
    baselines.to_csv(f"{args.save_prefix}_baselines.csv", index=False)
    rules_df.to_csv(f"{args.save_prefix}_rules.csv", index=False)

    print_outputs(observations, baselines, rules_df, train_fraction=args.train_fraction)


if __name__ == "__main__":
    main()
