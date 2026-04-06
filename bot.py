import requests
from datetime import datetime, timedelta
import pandas as pd  # optional, for easy CSV export

def get_candlesticks(ticker):
    base = "https://api.elections.kalshi.com/trade-api/v2"
    # Try live first, fall back to historical if needed
    for url in [f"{base}/markets/{ticker}/candlesticks",
                f"{base}/historical/markets/{ticker}/candlesticks"]:
        resp = requests.get(url).json()
        if "candlesticks" in resp:
            return resp["candlesticks"]
    return []

def analyze_series(series_ticker, coin_name, max_markets=200):
    base = "https://api.elections.kalshi.com/trade-api/v2"
    markets = requests.get(
        f"{base}/markets?series_ticker={series_ticker}&status=all&limit=100"
    ).json()["markets"]
    
    results = []
    for m in markets[:max_markets]:  # limit to avoid rate limits
        ticker = m["ticker"]
        close_time = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        result = m.get("result")  # "yes" or "no"
        
        candles = get_candlesticks(ticker)
        if not candles:
            continue
        
        # Sort candles by time
        candles = sorted(candles, key=lambda c: c["time"])
        
        entry_points = {
            "t_minus_5min": close_time - timedelta(minutes=5),
            "t_minus_3min": close_time - timedelta(minutes=3),
            "t_minus_90sec": close_time - timedelta(seconds=90),
        }
        
        row = {"ticker": ticker, "coin": coin_name, "result": result, "close_time": close_time}
        
        for label, target_time in entry_points.items():
            # Find closest candle before target time
            closest = max((c for c in candles if datetime.fromisoformat(c["time"].replace("Z","+00:00")) <= target_time), 
                         default=None, key=lambda c: c["time"])
            if closest:
                yes_price = float(closest.get("yes_close_dollars") or closest.get("yes_price_dollars", 0))
                row[label + "_yes_price"] = yes_price
                row[label + "_would_win"] = (yes_price >= 0.90 and result == "yes") or (yes_price <= 0.10 and result == "no")
        
        results.append(row)
    
    df = pd.DataFrame(results)
    df.to_csv(f"{coin_name}_15min_analysis.csv", index=False)
    return df

# Example usage — start here
btc_df = analyze_series("KXBTC15M", "BTC")
print(btc_df[["t_minus_3min_yes_price", "result", "t_minus_3min_would_win"]].head(20))
print("Win rate at 90¢+ at T-3min:", btc_df["t_minus_3min_would_win"].mean())