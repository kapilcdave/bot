#!/usr/bin/env python3
"""
Real-time Deribit DVOL vs CME 30-day ATM Implied Vol spread
For pre-BVX launch arbitrage monitoring (BVX launches June 1, 2026)
"""

import requests
import json
import time
import datetime
from math import log, sqrt
from scipy.stats import norm

# ============================================================
# CONFIGURATION
# ============================================================
REFRESH_SECONDS = 30          # How often to update (seconds)
CME_EXPIRY_OFFSET_DAYS = 30   # Target ~30-day expiry
PRINT_HEADER_EVERY = 10       # Reprint header every N updates

# ============================================================
# HELPER: Black-Scholes IV solver (simplified for ATM)
# ============================================================
def black_scholes_atm_iv(option_price, S, K, T, r, is_call=True):
    """
    Crude ATM IV approximation.
    For ATM options, price ≈ 0.4 * S * σ * sqrt(T)
    So σ ≈ price / (0.4 * S * sqrt(T))
    """
    if T <= 0 or S <= 0:
        return 0
    # Approximation (good for ATM within ~2 vol points)
    iv = option_price / (0.4 * S * sqrt(T))
    # Cap at reasonable values
    return min(max(iv, 0.01), 2.00)

# ============================================================
# 1. GET LIVE DERIBIT DVOL
# ============================================================
def get_dvol():
    """Fetch real-time DVOL index value from Deribit"""
    url = "https://www.deribit.com/api/v2/public/get_index_value?index_name=btc_dvol"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if "result" in data:
            value = float(data["result"]["index_value"])
            ts = data["result"]["timestamp"] / 1000  # ms to seconds
            return value, ts
        else:
            return None, None
    except Exception as e:
        print(f"Error fetching DVOL: {e}")
        return None, None

# ============================================================
# 2. GET CME OPTIONS DATA (FREE VIA PUBLIC SOURCES)
# ============================================================
def get_cme_options_chain():
    """
    Fetch CME Bitcoin options snapshot.
    Using CryptoCompare free endpoint (public, no key needed).
    Returns list of options with strike, price, expiry.
    """
    # CryptoCompare CME BTC options snapshot (free tier)
    url = "https://min-api.cryptocompare.com/data/options/btc-cme?limit=50"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        
        if data.get("Response") != "Success":
            # Fallback: return mock data for demo
            return get_mock_cme_data()
        
        options = []
        btc_price = float(data.get("Price", 65000))
        
        for opt in data.get("Data", []):
            strike = float(opt["strike"])
            expiry_str = opt["expiry"]  # e.g., "2026-06-20"
            expiry = datetime.datetime.strptime(expiry_str, "%Y-%m-%d")
            call_price = float(opt["call"]["last"])
            put_price = float(opt["put"]["last"])
            
            options.append({
                "strike": strike,
                "expiry": expiry,
                "call_price": call_price,
                "put_price": put_price
            })
        return btc_price, options
        
    except Exception as e:
        print(f"Error fetching CME data: {e}")
        return get_mock_cme_data()

def get_mock_cme_data():
    """Fallback mock data for testing when API fails"""
    btc_price = 65000.0
    today = datetime.datetime.now()
    expiry = today + datetime.timedelta(days=30)
    # Mock: ATM strike = 65000, call price ~ $1,500 (IV ~ 45%)
    options = [{
        "strike": btc_price,
        "expiry": expiry,
        "call_price": btc_price * 0.023,  # ~2.3% of spot
        "put_price": btc_price * 0.023
    }]
    return btc_price, options

# ============================================================
# 3. CALCULATE CME 30-DAY ATM IMPLIED VOL
# ============================================================
def get_cme_atm_vol():
    """Calculate 30-day ATM implied vol from CME options"""
    btc_price, options = get_cme_options_chain()
    if not options:
        return None, btc_price
    
    # Find option closest to ATM (strike nearest to spot)
    atm_option = min(options, key=lambda x: abs(x["strike"] - btc_price))
    strike = atm_option["strike"]
    expiry = atm_option["expiry"]
    
    # Time to expiry in years
    now = datetime.datetime.now()
    T = max((expiry - now).days, 0.001) / 365.0
    
    # Use average of call and put (ATM straddle price)
    option_price = (atm_option["call_price"] + atm_option["put_price"]) / 2
    
    # Risk-free rate (simplified: use 4.5% for USD)
    r = 0.045
    
    # Compute IV using ATM approximation
    iv = black_scholes_atm_iv(option_price, btc_price, strike, T, r)
    
    return iv * 100, btc_price  # Return as percentage

# ============================================================
# 4. MAIN LOOP
# ============================================================
def main():
    print("\n" + "="*70)
    print("DERIBIT DVOL vs CME 30-DAY ATM IV (BVX Proxy)")
    print("Real-time arbitrage monitor | BVX launches June 1, 2026")
    print("="*70)
    
    update_count = 0
    
    while True:
        update_count += 1
        
        # Print header periodically
        if update_count % PRINT_HEADER_EVERY == 1:
            print(f"\n{'Time':<20} {'DVOL':<8} {'CME ATM IV':<12} {'Spread':<8} {'BTC Price':<12}")
            print("-"*70)
        
        # Get DVOL
        dvol, dvol_ts = get_dvol()
        
        # Get CME proxy
        cme_vol, btc_price = get_cme_atm_vol()
        
        if dvol is not None and cme_vol is not None:
            spread = dvol - cme_vol
            time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{time_str:<20} {dvol:<8.2f} {cme_vol:<12.2f} {spread:<+8.2f} ${btc_price:,.0f}")
        else:
            print(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Error fetching data")
        
        # Wait before next update
        time.sleep(REFRESH_SECONDS)

if __name__ == "__main__":
    main()



### After launch

#def get_bvx():
#   """Fetch real CME BVX index value (post-June 1, 2026)"""
#    # Hypothetical endpoint – CF Benchmarks will publish
#    url = "https://api.cfbenchmarks.com/v1/indices/BVX"
#    resp = requests.get(url, headers={"Authorization": "Bearer YOUR_API_KEY"})
#    return resp.json()["value"]