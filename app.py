import os
import ccxt
import time
import threading
import requests
from flask import Flask, jsonify
import pandas as pd
from datetime import datetime, time as dtime
import pytz

app = Flask(__name__)

LEVERAGE = 125
TARGET_LOTS = 20
bot_started = False
is_position_active = False
current_position_symbol = None
expiry_day_shift = 0  # 0 for 0DTE (same day), 1+ for next day expiry if needed later

ist = pytz.timezone('Asia/Kolkata')

def send_telegram_alert(message):
    token = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"Telegram Error: {e}", flush=True)

def get_vwap_for_symbol(exchange, symbol):
    """
    Fetches candles for the option symbol and calculates VWAP.
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # VWAP calculation
        df['vwap'] = (df['volume'] * (df['high'] + df['low'] / 2)).cumsum() / df['volume'].cumsum()
        
        current_close = df['close'].iloc[-1]
        current_vwap = df['vwap'].iloc[-1]
        return current_close, current_vwap
    except Exception as e:
        print(f"Error calculating VWAP for {symbol}: {e}", flush=True)
        return None, None

def find_best_option_to_sell(exchange):
    """
    Scans option chain for 0DTE (or shifted expiry) and checks both Call and Put premiums vs VWAP.
    Returns (symbol, side, entry_price)
    """
    try:
        markets = exchange.load_markets()
        
        # Filter BTC options
        option_symbols = [symbol for symbol in markets if 'BTC' in symbol and ('C-' in symbol or 'P-' in symbol)]
        
        # For simplicity, let's find OTM options expiring today (0DTE) based on expiry_day_shift
        # Delta exchange option symbols usually contain expiry date like BTC-23SEP26-...
        now_utc = datetime.now(pytz.utc)
        target_date = now_utc.date() # Add days if expiry_day_shift > 0
        
        ticker = exchange.fetch_ticker('BTC/USD:BTC') # Underlying spot/future price reference
        btc_price = ticker['last']
        
        best_symbol = None
        best_side = 'sell' # Always selling options
        
        # We will look for an OTM Call and an OTM Put, check their VWAP, and pick the valid one
        for symbol in option_symbols:
            market = markets[symbol]
            strike = market.get('strike')
            option_type = market.get('optionType') # 'call' or 'put'
            
            if not strike:
                continue
                
            # Select OTM: Call strike > btc_price, Put strike < btc_price (roughly 2-5% away)
            is_otm_call = (option_type == 'call' and strike > btc_price * 1.01)
            is_otm_put = (option_type == 'put' and strike < btc_price * 0.99)
            
            if is_otm_call or is_otm_put:
                close_p, vwap_p = get_vwap_for_symbol(exchange, symbol)
                if close_p and vwap_p:
                    # Check condition: Premium (close price) < VWAP
                    if close_p < vwap_p:
                        print(f"✅ Found OTM {option_type.upper()} ({symbol}) -> Premium {close_p} is below VWAP {vwap_p:.2f}", flush=True)
                        return symbol, close_p
                        
        return None, None
    except Exception as e:
        print(f"Option Chain Scan Error: {e}", flush=True)
        return None, None

def manage_option_position(exchange, symbol, entry_price):
    """
    Monitors the active option sell position:
    - Exit if 90% profit reached (buying back at 10% of entry or tracking profit pct)
    - Exit if candle closes above VWAP
    """
    global is_position_active, expiry_day_shift
    print(f"🛡️ Managing Option Position for {symbol} | Entry Price: {entry_price}", flush=True)
    
    while is_position_active:
        try:
            current_p, vwap_p = get_vwap_for_symbol(exchange, symbol)
            if not current_p or not vwap_p:
                time.sleep(15)
                continue
                
            # For short option, profit percentage = ((Entry Price - Current Price) / Entry Price) * 100 * Leverage
            profit_pct = ((entry_price - current_p) / entry_price) * 100 * LEVERAGE
            
            print(f"📊 Option Monitor ({symbol}) -> Current Premium: {current_p} | VWAP: {vwap_p:.2f} | PnL: {profit_pct:.2f}%", flush=True)
            
            # Exit Condition 1: 90% Profit Reached (Full Exit)
            if profit_pct >= 90:
                print(f"🎯 90% Profit Target Reached! Buying back {TARGET_LOTS} lots to close position...", flush=True)
                exchange.create_order(symbol, 'market', 'buy', TARGET_LOTS)
                send_telegram_alert(f"✅ *Target Hit (90% Profit)!*\nClosed option sell position on {symbol} at premium {current_p}")
                
                is_position_active = False
                expiry_day_shift += 1  # Next trades will shift to next day expiry rule as requested
                break
                
            # Exit Condition 2: Candle closes above VWAP
            if current_p > vwap_p:
                print(f"⚠️ Candle closed above VWAP! Exiting option position...", flush=True)
                exchange.create_order(symbol, 'market', 'buy', TARGET_LOTS)
                send_telegram_alert(f"⚠️ *VWAP Exit Triggered!*\nClosed option position on {symbol} as premium crossed above VWAP.")
                
                is_position_active = False
                expiry_day_shift += 1
                break
                
            time.sleep(15)
        except Exception as e:
            print(f"Position Management Error: {e}", flush=True)
            time.sleep(15)

def option_bot_loop():
    global is_position_active, current_position_symbol
    print("🤖 BTC 0DTE Option Selling Bot Initialized...", flush=True)
    time.sleep(5)
    
    try:
        api_key = os.getenv('DELTA_API_KEY', '').strip()
        secret_key = os.getenv('DELTA_API_SECRET', '').strip()

        exchange = ccxt.delta({
            'apiKey': api_key,
            'secret': secret_key,
            'enableRateLimit': True,
            'timeout': 10000,
            'urls': {
                'api': {
                    'public': 'https://api.india.delta.exchange',
                    'private': 'https://api.india.delta.exchange',
                }
            }
        })
        exchange.load_markets()
        
        try:
            exchange.set_leverage(LEVERAGE, 'BTCUSD')
        except:
            pass

        while True:
            now = datetime.now(ist)
            t = now.time()
            
            # 1. Square-off at 4:44 PM IST
            if t.hour == 16 and t.minute == 44:
                if is_position_active and current_position_symbol:
                    print("⏰ 4:44 PM IST! Square-off all running option positions...", flush=True)
                    try:
                        exchange.create_order(current_position_symbol, 'market', 'buy', TARGET_LOTS)
                        send_telegram_alert(f"⏰ *4:44 PM Square-off Executed* for {current_position_symbol}")
                    except Exception as sq_err:
                        print(f"Square-off error: {sq_err}", flush=True)
                    is_position_active = False
                    current_position_symbol = None
                time.sleep(60)
                continue
                
            # 2. No Trade Zone: 4:45 PM to 6:29 PM IST
            if dtime(16, 45) <= t < dtime(18, 30):
                print("⏳ No-Trade Zone active (4:45 PM - 6:29 PM). Sleeping...", flush=True)
                time.sleep(60)
                continue
                
            # 3. Entry at exactly 6:30 PM IST (if no position is active)
            if not is_position_active and t.hour == 18 and t.minute >= 30:
                print("🔍 6:30 PM reached. Scanning OTM Call and Put premiums vs VWAP...", flush=True)
                
                symbol, entry_price = find_best_option_to_sell(exchange)
                if symbol and entry_price:
                    print(f"🚀 Placing Sell Order for {TARGET_LOTS} lots of {symbol} at 125x leverage...", flush=True)
                    response = exchange.create_order(symbol, 'market', 'sell', TARGET_LOTS)
                    
                    is_position_active = True
                    current_position_symbol = symbol
                    
                    send_telegram_alert(f"🚨 *0DTE Option Sell Executed!*\nSymbol: {symbol}\nLots: {TARGET_LOTS}\nLeverage: {125}x\nEntry Premium: {entry_price}")
                    
                    # Start monitoring thread
                    threading.Thread(target=manage_option_position, args=(exchange, symbol, entry_price), daemon=True).start()
                else:
                    print("⏳ No suitable OTM option found below VWAP right now. Retrying in 60s...", flush=True)
                    
            time.sleep(30)
            
    except Exception as e:
        print(f"Fatal Option Bot Error: {e}", flush=True)

@app.before_request
def start_bot_once():
    global bot_started
    if not bot_started:
        bot_started = True
        threading.Thread(target=option_bot_loop, daemon=True).start()

@app.route('/')
def home():
    return jsonify({
        "status": "BTC 0DTE Option Selling Bot Running",
        "position_active": is_position_active,
        "active_symbol": current_position_symbol,
        "leverage": LEVERAGE,
        "lots": TARGET_LOTS
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
