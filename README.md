# Kalshi Crypto Hedge Bot

This repo now has three separate tools:

- `kalshi_15min_scalper_analysis.py`: historical analysis and reporting
- `bot.py`: a dual-sided Kalshi 15-minute crypto bot with a separate hedge engine
- `reverse_engineer.py`: a screenshot-driven scaffold for modeling a dual-sided "incremental pair" bot console
- `hedge_engine.py`: a Kalshi-oriented hedge decision engine for 15-minute crypto binaries

## What Changed

The old `bot.py` made trading decisions from public candlestick backfills and invalid market queries. That is not a system I would trust for automation.

The replacement bot uses:

- current `open` markets only
- the live orderbook for pricing and available size
- explicit time-to-expiry filters
- hard risk limits on per-trade and total exposure
- `paper` mode by default

## Bot Strategy

The live bot is intentionally narrow:

- scans Kalshi 15-minute crypto markets only
- maintains YES and NO inventory independently
- uses a simple GBM fair-value estimate to decide when one side is cheap enough to accumulate
- allows both pair-building and side-pressing behavior
- runs a separate hedge engine that decides when to buy insurance on the weak side
- stops pressing once terminal PnL is locked on both outcomes

This is still not guaranteed alpha. It is a safer engineering scaffold for a two-sided Kalshi market-making and hedging workflow, not proof of profitability.

## Modes

`paper` mode:

- default
- no authenticated order placement
- simulates fills in memory from current ask prices

`live` mode:

- requires Kalshi API credentials
- submits IOC limit buy orders through the authenticated portfolio order endpoint
- requires `KALSHI_ALLOW_LIVE_TRADING=true`

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
export KALSHI_SERIES=KXBTC15M
export KALSHI_SCAN_INTERVAL_SECONDS=5
export KALSHI_MIN_MINUTES_TO_CLOSE=1
export KALSHI_MAX_MINUTES_TO_CLOSE=15
export KALSHI_BASE_ORDER_SIZE=2
export KALSHI_MAX_SIDE_SHARES=25
export KALSHI_MAX_TOTAL_COST=60
export KALSHI_FAIR_VALUE_EDGE_CENTS=4
export KALSHI_PRESS_EDGE_CENTS=6
export KALSHI_CHEAP_HEDGE_PRICE_CAP=0.25
export KALSHI_INVENTORY_IMBALANCE_TRIGGER=2
export KALSHI_PAPER_FILL_OFFSET_CENTS=0
```

Live mode also needs:

```bash
export KALSHI_API_KEY_ID=your_key_id
export KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/private_key.pem
export KALSHI_ALLOW_LIVE_TRADING=true
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

Render the reverse-engineered dashboard samples:

```bash
python reverse_engineer.py --sample both
```

## Why This Is Better Than The Old Script

The automation path should not depend on:

- a single directional position model
- hand-wavy hedge logic
- historical candlestick proxies for executable prices

Instead, `bot.py` now:

- uses current open Kalshi markets
- values both outcomes explicitly
- keeps separate YES and NO inventory
- runs a distinct hedge decision engine
- defaults to paper mode

## Default Universe

The bot now defaults to:

- `KXBTC15M`

## Remaining Gaps

If you want this to be production-grade rather than just sane:

- add WebSocket market data instead of REST polling
- persist positions and fills across restarts
- reconcile actual fills from `Get Orders` and `Get Positions`
- add emergency exits before expiry when the signal breaks
- add post-trade analytics from real fills, not just settlement outcomes
- add alerting and kill switches

## Reverse-Engineering Notes

The screenshots you shared are not showing a simple directional bot. They imply a state machine closer to:

- accumulate inventory on both sides
- track the net payout if `YES` wins vs if `NO` wins
- keep posting passive hedge orders while occasionally crossing the spread on the favored side
- enter a `parity` or `profit lock` mode once both terminal outcomes are non-negative
- halt trading once the book is hard-locked

`reverse_engineer.py` models that state explicitly:

- dual-sided inventory with average cost
- pending GTC and batching orders
- "if yes wins / if no wins" payout math
- break-even deficit math
- guard, parity, and hard-lock flags

That file is a scaffold, not a live strategy. Its purpose is to make the screenshot's mechanics concrete enough that real exchange adapters, fill handlers, and signal logic can be added without guessing at the shape of the state.

## Kalshi Hedge Engine

You clarified that the real target is `Kalshi`, specifically US-accessible 15-minute crypto markets. The hedge logic is portable, but execution details are not. `hedge_engine.py` is therefore written as a Kalshi-side component:

- inputs are current `YES` and `NO` prices from the Kalshi order book
- time pressure is tied to seconds elapsed within a 15-minute market
- hedge sizing is based on payout math for Kalshi binary contracts
- the engine assumes an existing execution loop is responsible for IOC/FOK order submission and fill reconciliation

The intended integration path is:

1. `bot.py` or a new Kalshi market-maker loop maintains the live book and position state.
2. That loop feeds `HedgeBook` and `HedgeInputs` into `HedgeEngine.decide(...)` every tick.
3. If a hedge is approved, the execution layer places the corresponding Kalshi order and reconciles the fill before updating state.

This keeps the hedge engine exchange-specific where it needs to be, without hardcoding API calls into the decision model itself.

## Reference

Official docs used for the design:

- https://docs.kalshi.com/welcome
- https://docs.kalshi.com/getting_started/quick_start_websockets
- https://docs.kalshi.com/api-reference/market/get-markets
- https://docs.kalshi.com/api-reference/orders/create-order
- https://docs.kalshi.com/api-reference/portfolio/get-positions
