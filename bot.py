import logging
import time
import csv
import os
from datetime import datetime
import requests
from kalshi_python import ApiClient, Configuration, MarketsApi

# --- CONFIGURATION ---
CITY_COORDS = {
    "KXHIGHNY": {"lat": 40.71, "lon": -74.00}, # NYC (JFK/Central Park)
    "KXHIGHCHI": {"lat": 41.87, "lon": -87.62}, # Chicago (O'Hare)
    "KXHIGHMIA": {"lat": 25.76, "lon": -80.19}, # Miami
}
LOG_FILE = "whale_monitor.log"
DATA_CSV = "predictions_vs_market.csv"
CHECK_INTERVAL = 300  # 5 minutes

# Setup Robust Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)

class WeatherWhale:
    def __init__(self):
        # In 2026, always use environment variables for keys!
        self.api_key_id = os.getenv("KALSHI_KEY_ID")
        self.private_key_path = "private_key.pem"
        self.client = self._init_kalshi()

    def _init_kalshi(self):
        try:
            with open(self.private_key_path, 'r') as f:
                private_key = f.read()
            config = Configuration(
                host="https://api.elections.kalshi.com/trade-api/v2",
                api_key_id=self.api_key_id,
                private_key_pem=private_key
            )
            return ApiClient(config)
        except Exception as e:
            logging.error(f"Failed to init Kalshi: {e}")
            return None

    def get_model_probability(self, lat, lon, threshold_f):
        """Fetches 31 GFS ensemble members and calculates exceedance probability."""
        try:
            url = f"https://api.open-meteo.com/v1/ensemble?latitude={lat}&longitude={lon}&hourly=temperature_2m&models=gfs_seamless"
            res = requests.get(url, timeout=10).json()
            
            # Extract max temp for today for each of the 31 ensemble members
            # (Simplification: using current day's 24-hour max)
            member_maxes = []
            for i in range(31):
                member_data = res['hourly'][f'temperature_2m_member{i:02d}']
                # Convert C to F: (C * 9/5) + 32
                max_f = (max(member_data[:24]) * 9/5) + 32
                member_maxes.append(max_f)
            
            count_above = sum(1 for temp in member_maxes if temp >= threshold_f)
            return count_above / 31.0
        except Exception as e:
            logging.warning(f"Weather API error: {e}")
            return None

    def run_loop(self):
        logging.info("Whale Engine Started. Monitoring Markets...")
        while True:
            try:
                markets_api = MarketsApi(self.client)
                for series, coords in CITY_COORDS.items():
                    # Get open markets for this city
                    response = markets_api.get_markets(status="open", series_ticker=series)
                    
                    for m in response.markets:
                        # Logic: Market 'KXHIGHNY-26MAR-T75' means "Will it be >= 75F?"
                        # We extract the threshold from the ticker or metadata
                        threshold = float(m.cap) # Or parse from ticker string
                        
                        model_prob = self.get_model_probability(coords['lat'], coords['lon'], threshold)
                        market_price = m.yes_ask / 100.0  # Convert cents to 0-1 probability
                        
                        if model_prob is not None:
                            edge = model_prob - market_price
                            self.log_data(m.ticker, threshold, market_price, model_prob, edge)
                            
                            if edge > 0.08:
                                logging.info(f"🔥 OPPORTUNITY: {m.ticker} | Edge: {edge:.2%}")

                time.sleep(CHECK_INTERVAL)
            except Exception as e:
                logging.error(f"Critical Loop Error: {e}")
                time.sleep(30) # Cool down before retry

    def log_data(self, ticker, threshold, mkt_p, mod_p, edge):
        file_exists = os.path.isfile(DATA_CSV)
        with open(DATA_CSV, 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "ticker", "threshold", "market_p", "model_p", "edge", "actual_outcome"])
            writer.writerow([datetime.now(), ticker, threshold, mkt_p, mod_p, edge, "PENDING"])

if __name__ == "__main__":
    bot = WeatherWhale()
    bot.run_loop()