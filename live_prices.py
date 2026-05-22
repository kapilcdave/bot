import asyncio
import json
import os
import sys
import requests
import websockets
from dotenv import load_dotenv
from kalshi_auth import get_auth_headers
from datetime import datetime
from zoneinfo import ZoneInfo

load_dotenv()

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
REST_URL = "https://external-api.kalshi.com/trade-api/v2"
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

def get_time_left(ticker):
    try:
        parts = ticker.split("-")
        if len(parts) < 2:
            return None
        dt_str = parts[1]
        if len(dt_str) < 11:
            return None
            
        yy = int(dt_str[0:2]) + 2000
        mon_str = dt_str[2:5].upper()
        dd = int(dt_str[5:7])
        hh = int(dt_str[7:9])
        mm = int(dt_str[9:11])
        
        mon = MONTHS.get(mon_str)
        if not mon:
            return None
            
        expiry_dt = datetime(yy, mon, dd, hh, mm, tzinfo=ZoneInfo("America/New_York"))
        now_dt = datetime.now(ZoneInfo("America/New_York"))
        
        return (expiry_dt - now_dt).total_seconds()
    except Exception:
        return None

def format_time_left(seconds):
    if seconds is None:
        return "Unknown"
    if seconds < 0:
        return "Expired"
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{mins}m {secs:02d}s"

def format_cent_price(dollar_val):
    if dollar_val is None:
        return "N/A"
    try:
        val = float(dollar_val) * 100
        if val <= 0:
            return "N/A"
        rounded = round(val, 1)
        if rounded.is_integer():
            return str(int(rounded))
        return str(rounded)
    except ValueError:
        return "N/A"

def format_no_cent_price(yes_bid_dollars):
    if yes_bid_dollars is None:
        return "N/A"
    try:
        if float(yes_bid_dollars) <= 0:
            return "N/A"
        val = (1.0 - float(yes_bid_dollars)) * 100
        rounded = round(val, 1)
        if rounded.is_integer():
            return str(int(rounded))
        return str(rounded)
    except ValueError:
        return "N/A"

def get_active_tickers():
    if not API_KEY_ID or not PRIVATE_KEY_PATH:
        print("Warning: KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH is not set in .env")
        return ["KXBTC15M-25MAR15-B100000"]
        
    path = "/markets"
    headers = get_auth_headers(API_KEY_ID, PRIVATE_KEY_PATH, "GET", path)
    response = requests.get(
        f"{REST_URL}{path}",
        headers=headers,
        params={"series_ticker": "KXBTC15M", "status": "open"}
    )
    if response.status_code == 200:
        markets = response.json().get("markets", [])
        return [m.get("ticker") for m in markets]
    return ["KXBTC15M-25MAR15-B100000"]

async def listen_for_quit():
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line.strip().lower() == 'q':
            print("\nQuitting...")
            os._exit(0)

async def watch_prices():
    if not API_KEY_ID or not PRIVATE_KEY_PATH:
        print("Error: KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in your .env file to watch live prices.")
        return

    path = "/trade-api/ws/v2"

    asyncio.create_task(listen_for_quit())
    print("Press 'q' + Enter to stop.\n")

    while True:
        try:
            print("Connecting to Kalshi live feed...")
            headers = get_auth_headers(API_KEY_ID, PRIVATE_KEY_PATH, "GET", path)

            async with websockets.connect(WS_URL, additional_headers=headers) as ws:
                subscribe_msg = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["ticker"]
                    }
                }
                await ws.send(json.dumps(subscribe_msg))
                print("Subscribed to global ticker feed. Waiting for ticks...")

                async for message in ws:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    
                    if msg_type == "subscribed":
                        print("Subscription confirmed.")
                        continue
                    
                    if msg_type == "ticker":
                        ticker_data = data.get("msg", {})
                        t = ticker_data.get("market_ticker")
                        if t and t.startswith("KXBTC15M-"):
                            yes_ask_val = ticker_data.get("yes_ask_dollars")
                            yes_bid_val = ticker_data.get("yes_bid_dollars")
                            
                            yes_price = format_cent_price(yes_ask_val)
                            no_price = format_no_cent_price(yes_bid_val)
                            
                            time_left = get_time_left(t)
                            time_left_str = format_time_left(time_left)
                            
                            print(f"[{t}] UP: {yes_price}c | DOWN: {no_price}c | Time Left: {time_left_str}")
                    elif msg_type == "error":
                        print(f"Error: {data}")
        except websockets.exceptions.ConnectionClosed:
            print("Connection closed. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Connection error: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(watch_prices())
