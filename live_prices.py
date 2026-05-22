import asyncio
import json
import os
import requests
import websockets
from dotenv import load_dotenv
from kalshi_auth import get_auth_headers

load_dotenv()

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
REST_URL = "https://external-api.kalshi.com/trade-api/v2"
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

def get_active_tickers():
    path = "/markets"
    headers = get_auth_headers(API_KEY_ID, PRIVATE_KEY_PATH, "GET", path)
    response = requests.get(f"{REST_URL}{path}", headers=headers, params={"limit": 10})
    if response.status_code == 200:
        markets = response.json().get("markets", [])
        return [m.get("ticker") for m in markets]
    return ["KXBTC-25MAR15-B100000"]

async def watch_prices():
    print("Connecting to Kalshi live feed...")
    
    path = "/trade-api/ws/v2"
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

        try:
            async for message in ws:
                data = json.loads(message)
                msg_type = data.get("type")
                
                if msg_type == "subscribed":
                    print("Subscription confirmed.")
                    continue
                
                if msg_type == "ticker":
                    ticker_data = data.get("msg", {})
                    t = ticker_data.get("market_ticker")
                    yes_price = ticker_data.get("yes_ask", "N/A")
                    no_price = ticker_data.get("no_ask", "N/A")
                    
                    print(f"[{t}] YES: {yes_price}c | NO: {no_price}c")
                elif msg_type == "error":
                    print(f"Error: {data}")
        except websockets.exceptions.ConnectionClosed:
            print("Connection closed")
        except websockets.exceptions.ConnectionClosed:
            print("Connection closed")

if __name__ == "__main__":
    asyncio.run(watch_prices())
