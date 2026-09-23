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
expiry_day_shift = 0

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
    Fetches 15m candles for the specific option symbol and calculates VWAP.
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # VWAP calculation
        df['vwap'] = (df['volume'] * (df['high'] + df['low'] / 2)).cumsum() / df['volume'].cumsum()
        
        current_close = df['close'].iloc[-1]
        current_vwap = df['vwap'].iloc[-1]
        return current_close, current_vwap
    except Exception as e:
        print(f"Error calculating VWAP for {symbol}: {e}", flush=True)
        return None, None

def find_best_dynamic_option_to_sell(exchange):
    """
    Dynamically fetches live BTC price, filters today's 0DTE options chain,
    selects OTM contracts, and checks if their premium is below VWAP.
    """
    try:
        markets = exchange.load_markets()
        
        # Fetch current live BTC underlying price
        ticker = exchange.fetch_ticker('BTC/USD:BTC')
        btc_price = ticker['last']
        print(f"📊 Live BTC Reference Price for Option Selection: {btc_price}", flush=True)
        
        # Filter BTC option symbols
        option_symbols = [symbol for symbol in markets if 'BTC' in symbol and ('C-' in symbol or 'P-' in symbol)]
        
        # Let's find today's date string format used in Delta symbols (e.g., 24SEP26 or similar from market info)
        # We look through available options to find the nearest/today's expiry (0DTE)
        valid_options = []
        for symbol in option_symbols:
            market = markets[symbol]
            strike = market.get('strike')
            option_type = market.get('optionType') # 'call' or 'put'
            expiry_date_str = market.get('expiry') # timestamp or string depending on CCXT
            
            if not strike or not option_type:
                continue
                
            # Dynamic OTM Filter: Call strike > btc_price (e.g., ~1% to 2% OTM), Put strike < btc_price
            is_otm_call = (option_type == 'call' and strike > btc_price * 1.005)
            is_otm_put = (option_type == 'put' and strike < btc_price * 0.995)
            
            if is_otm_call or is_otm_put:
                valid_options.append((symbol, option_type, strike))
                
        # Sort or iterate to check VWAP for potential OTM candidates
        for symbol, option_type, strike in valid_options:
            close_p, vwap_p = get_vwap_for_symbol(exchange, symbol)
            if close_p and vwap_p:
                # Check condition: Premium (close price) < VWAP
                if close_p < vwap_p:
                    print(f"✅ Selected Dynamic OTM {option_type.upper()} -> Symbol: {symbol} | Strike: {strike} | Premium: {close_p} < VWAP: {vwap_p:.2f}", flush=True)
                    return symbol, close_p
                    
        print("⚠️ No OTM option found with premium below VWAP right now.", flush=True)
        return None, None
    except Exception as e:
        print(f"Dynamic Option Chain Scan Error: {e}", flush=True)
        return None, None

def manage_option_position(exchange, symbol, entry_price):
    """
    Monitors active option position:
    - Full exit if 90% profit reached
    - Full exit if 15m candle closes above VWAP
    """
    global is_position_active, expiry_day_shift
    print(f"🛡️ Managing Dynamic Option Position for {symbol} | Entry Premium: {entry_price}", flush=True)
    
    while is_position_active:
        try:
            current_p, vwap_p = get_vwap_for_symbol(exchange, symbol)
            if not current_p or not vwap_p:
                time.sleep(15)
                continue
                
            # Short option profit percentage = ((Entry Price - Current Price) / Entry Price) * 100 * Leverage
            profit_pct = ((entry_price - current_p) / entry_price) * 100 * LEVERAGE
            
            print(f"📊 Live Monitor [{symbol}] -> Premium: {current_p} | VWAP: {vwap_p:.2f} | PnL: {profit_pct:.2f}%", flush=True)
            
            # Exit Condition 1: 90% Profit Reached (Full Exit)
            if profit_pct >= 90:
                print(f"🎯 90% Profit Reached! Buying back {TARGET_LOTS} lots to close position...", flush=True)
                exchange.create_order(symbol, 'market', 'buy', TARGET_LOTS)
                send_telegram_alert(f"✅ *Target Hit (90% Profit)!*\nClosed dynamic option position on {symbol} at premium {current_p}")
                
                is_position_active = False
                expiry_day_shift += 1
                break
                
            # Exit Condition 2: Candle closes above VWAP
            if current_p > vwap_p:
                print(f"⚠️ Candle closed above VWAP! Exiting option position...", flush=True)
                exchange.create_order(symbol, 'market', 'buy', TARGET_LOTS)
                send_telegram_alert(f"⚠️ *VWAP Exit Triggered!*\nClosed position on {symbol} as premium crossed above VWAP.")
                
                is_position_active = False
                expiry_day_shift += 1
                break
                
            time.sleep(15)
        except Exception as e:
            print(f"Position Management Error: {e}", flush=True)
            time.sleep(15)

def option_bot_loop():
    global is_position_active, current_position_symbol
    print("🤖 BTC Dynamic 0DTE Option Selling Bot Initialized...", flush=True)
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
                
            # 2. No-Trade Zone: 4:45 PM to 6:29 PM IST
            if dtime(16, 45) <= t < dtime(18, 30):
                print("⏳ No-Trade Zone active (4:45 PM - 6:29 PM). Sleeping...", flush=True)
                time.sleep(60)
                continue
                
            # 3. Entry at exactly 6:30 PM IST (if no position is active)
            if not is_position_active and t.hour == 18 and t.minute >= 30:
                print("🔍 6:30 PM reached. Dynamically scanning live OTM options chain...", flush=True)
                
                symbol, entry_price = find_best_dynamic_option_to_sell(exchange)
                if symbol and entry_price:
                    print(f"🚀 Placing Sell Order for {TARGET_LOTS} lots of {symbol} at 125x leverage...", flush=True)
                    response = exchange.create_order(symbol, 'market', 'sell', TARGET_LOTS)
                    
                    is_position_active = True
                    current_position_symbol = symbol
                    
                    send_telegram_alert(f"🚨 *Dynamic 0DTE Option Sell Executed!*\nSymbol: {symbol}\nLots: {TARGET_LOTS}\nLeverage: {LEVERAGE}x\nEntry Premium: {entry_price}")
                    
                    # Start monitoring thread
                    threading.Thread(target=manage_option_position, args=(exchange, symbol, entry_price), daemon=True).start()
                else:
                    print("⏳ No suitable dynamic OTM option found below VWAP right now. Retrying in 60s...", flush=True)
                    
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
        "status": "BTC Dynamic 0DTE Option Selling Bot Running",
        "position_active": is_position_active,
        "active_symbol": current_position_symbol,
        "leverage": LEVERAGE,
        "lots": TARGET_LOTS
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
