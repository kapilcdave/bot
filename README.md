# Kalshi Crypto Expiry Scalper

This repo now has two separate tools:

- `kalshi_15min_scalper_analysis.py`: historical analysis and reporting
- `bot.py`: a safer real-time scalping bot framework for paper or live trading

## What Changed

The old `bot.py` made trading decisions from public candlestick backfills and invalid market queries. That is not a system I would trust for automation.

The replacement bot uses:

- current `open` markets only
- the live orderbook for pricing and available size
- explicit time-to-expiry filters
- hard risk limits on per-trade and total exposure
- `paper` mode by default

## Strategy

The bot is intentionally narrow:

- scans 15-minute crypto series only
- looks for markets closing in the next 1 to 5 minutes
- identifies the current favorite side from the orderbook
- only buys the favorite when all of these hold:
  - favorite ask is within the configured price band
  - bid/ask spread is tight
  - top-of-book bid support is large enough
  - enough size exists to lift the ask
  - open interest and volume clear minimum thresholds

This is still not guaranteed alpha. It is a disciplined execution framework around the "late favorite into expiry" idea, not proof that the idea is profitable.

## Modes

`paper` mode:

- default
- no authenticated trading calls
- simulates entries and settlement PnL in memory

`live` mode:

- requires Kalshi API credentials
- submits IOC limit buy orders through the authenticated portfolio order endpoint

## Setup

Create and activate the local virtualenv if needed:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Environment

Optional settings:

```bash
export KALSHI_MODE=paper
export KALSHI_ENV=prod
export KALSHI_SERIES=KXBTC15M,KXETH15M,KXSOL15M
export KALSHI_SCAN_INTERVAL_SECONDS=5
export KALSHI_MIN_MINUTES_TO_CLOSE=1
export KALSHI_MAX_MINUTES_TO_CLOSE=5
export KALSHI_TARGET_MINUTES_TO_CLOSE=3
export KALSHI_MIN_FAVORITE_PRICE_CENTS=88
export KALSHI_MAX_ENTRY_PRICE_CENTS=95
export KALSHI_MAX_SPREAD_CENTS=2
export KALSHI_MIN_BID_SUPPORT_CONTRACTS=20
export KALSHI_MIN_ASK_LIQUIDITY_CONTRACTS=10
export KALSHI_MIN_OPEN_INTEREST_CONTRACTS=50
export KALSHI_MIN_VOLUME_CONTRACTS=100
export KALSHI_CONTRACTS_PER_TRADE=10
export KALSHI_MAX_CONCURRENT_POSITIONS=2
export KALSHI_MAX_TRADE_NOTIONAL_DOLLARS=15
export KALSHI_MAX_TOTAL_EXPOSURE_DOLLARS=30
```

Live mode also needs:

```bash
export KALSHI_API_KEY_ID=your_key_id
export KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/private_key.pem
```

## Usage

One decision cycle:

```bash
python bot.py --once
```

Continuous paper trading:

```bash
python bot.py
```

Continuous live trading against demo:

```bash
KALSHI_MODE=live KALSHI_ENV=demo python bot.py
```

Continuous live trading against production:

```bash
KALSHI_MODE=live KALSHI_ENV=prod python bot.py
```

## Why This Is Better Than The Old Script

The automation path should not depend on:

- historical candlestick proxies for executable prices
- invalid `status=all` market queries
- assumptions that every detected favorite is still tradeable

Instead, `bot.py` makes decisions from the current book and only trades when the book can support the intended entry.

## Remaining Gaps

If you want this to be production-grade rather than just sane:

- add WebSocket market data instead of REST polling
- persist positions and fills across restarts
- reconcile actual fills from `Get Orders` and `Get Positions`
- add emergency exits before expiry when the signal breaks
- add post-trade analytics from real fills, not just settlement outcomes
- add alerting and kill switches

## Reference

Official docs used for the design:

- https://docs.kalshi.com/welcome
- https://docs.kalshi.com/getting_started/quick_start_websockets
- https://docs.kalshi.com/api-reference/market/get-markets
- https://docs.kalshi.com/api-reference/orders/create-order
- https://docs.kalshi.com/api-reference/portfolio/get-positions
