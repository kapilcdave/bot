# BTC Kalshi Mid-Market Sniper
This repository is intentionally reduced to a single strategy:
- Estimate institutional probability from Deribit options (`get_deribit_true_probability`).
- Compare that fair probability against live Kalshi YES/NO pricing.
- Snipe only in the minute-2 to minute-11 window of each market.
- Buy NO when `true_no_prob - kalshi_no_price` exceeds threshold.

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Config
Optional environment variables:
```bash
export KALSHI_WINDOW_START_MINUTE=2
export KALSHI_WINDOW_END_MINUTE=11
export KALSHI_MIN_MISPRICING_EDGE=0.10
export KALSHI_BANKROLL_DOLLARS=20
export KALSHI_LOOP_SECONDS=2
```

## Run
One cycle:
```bash
python bot.py --once
```

Continuous:
```bash
python bot.py
```

With explicit market-open timestamp:
```bash
python bot.py --market-open-utc 2025-05-18T23:00:00+00:00
```

## Notes
- `get_deribit_true_probability` and `get_kalshi_market_prices` are intentionally placeholders.
- Replace those methods with your live Deribit/Kalshi integrations.
