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
HARD_SL_PCT = -40.0  # Hard stop-loss limit (exits immediately if loss hits 40%)

bot_started = False
is_position_active = False
current_position_symbol = None
expiry_day_shift = 0

# Dashboard live metrics tracking
dashboard_status = {
    "status": "Initializing...",
    "last_updated": "",
    "live_btc_price": 0,
    "position_active": False,
    "active_symbol": None,
    "entry_price": 0,
    "current_price": 0,
    "current_vwap": 0,
    "pnl_percentage": 0,
    "message": "Bot is starting up..."
}

ist = pytz.timezone('Asia/Kolkata')

def update_dashboard(status_text, btc_p=0, pos_act=False, sym=None, entry_p=0, curr_p=0, vwap_p=0, pnl=0):
    global dashboard_status
    dashboard_status = {
        "status": status_text,
        "last_updated": datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST'),
        "live_btc_price": btc_p,
        "position_active": pos_act,
        "active_symbol": sym,
        "entry_price": entry_p,
        "current_price": curr_p,
        "current_vwap": round(vwap_p, 2) if vwap_p else 0,
        "pnl_percentage": round(pnl, 2),
        "message": status_text
    }

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
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['vwap'] = (df['volume'] * (df['high'] + df['low'] / 2)).cumsum() / df['volume'].cumsum()
        current_close = df['close'].iloc[-1]
        current_vwap = df['vwap'].iloc[-1]
        return current_close, current_vwap
    except Exception as e:
        print(f"Error calculating VWAP for {symbol}: {e}", flush=True)
        return None, None

def find_strict_first_otm_option(exchange):
    try:
        markets = exchange.load_markets()
        ticker = exchange.fetch_ticker('BTC/USD:BTC')
        btc_price = ticker['last']
        print(f"📊 Live BTC Reference Price: {btc_price}", flush=True)
        
        option_symbols = [symbol for symbol in markets if 'BTC' in symbol and ('C-' in symbol or 'P-' in symbol)]
        calls = []
        puts = []
        
        for symbol in option_symbols:
            market = markets[symbol]
            strike = market.get('strike')
            option_type = market.get('optionType')
            
            if not strike or not option_type:
                continue
                
            if option_type == 'call' and strike > btc_price:
                calls.append((strike, symbol))
            elif option_type == 'put' and strike < btc_price:
                puts.append((strike, symbol))
                
        calls.sort(key=lambda x: x[0])
        puts.sort(key=lambda x: x[0], reverse=True)
        
        candidates = []
        if calls:
            candidates.append(calls[0])
        if puts:
            candidates.append(puts[0])
            
        for strike, symbol in candidates:
            close_p, vwap_p = get_vwap_for_symbol(exchange, symbol)
            if close_p and vwap_p:
                if close_p < vwap_p:
                    print(f"✅ Found 1st OTM Option -> Symbol: {symbol} | Strike: {strike} | Premium: {close_p} < VWAP: {vwap_p:.2f}", flush=True)
                    return symbol, close_p, btc_p
                    
        print("⚠️ No 1st OTM option found with premium below VWAP right now.", flush=True)
        return None, None, btc_p
    except Exception as e:
        print(f"Strict 1st OTM Scan Error: {e}", flush=True)
        return None, None, 0

def manage_option_position(exchange, symbol, entry_price):
    global is_position_active, expiry_day_shift
    print(f"🛡️ Managing 1st OTM Option Position for {symbol} | Entry Premium: {entry_price}", flush=True)
    
    while is_position_active:
        try:
            current_p, vwap_p = get_vwap_for_symbol(exchange, symbol)
            ticker = exchange.fetch_ticker('BTC/USD:BTC')
            btc_p = ticker['last'] if ticker else 0
            
            if not current_p or not vwap_p:
                update_dashboard("Monitoring Position (Data error)", btc_p, True, symbol, entry_price, 0, 0, 0)
                time.sleep(15)
                continue
                
            # Short option profit/loss percentage calculation
            profit_pct = ((entry_price - current_p) / entry_price) * 100 * LEVERAGE
            
            print(f"📊 Live Monitor [{symbol}] -> Premium: {current_p} | VWAP: {vwap_p:.2f} | PnL: {profit_pct:.2f}%", flush=True)
            update_dashboard("Position Active & Monitored", btc_p, True, symbol, entry_price, current_p, vwap_p, profit_pct)
            
            # Exit Condition 1: 90% Profit Reached (Full Exit)
            if profit_pct >= 90:
                print(f"🎯 90% Profit Reached! Closing position...", flush=True)
                exchange.create_order(symbol, 'market', 'buy', TARGET_LOTS)
                send_telegram_alert(f"✅ *Target Hit (90% Profit)!*\nClosed position on {symbol} at premium {current_p}")
                is_position_active = False
                expiry_day_shift += 1
                update_dashboard("Position Closed (90% Target Hit)", btc_p, False, None, 0, current_p, vwap_p, profit_pct)
                break
                
            # Exit Condition 2: Hard Stop-Loss Triggered (-40%)
            if profit_pct <= HARD_SL_PCT:
                print(f"🛑 Hard Stop-Loss Hit ({profit_pct:.2f}%)! Exiting position immediately...", flush=True)
                exchange.create_order(symbol, 'market', 'buy', TARGET_LOTS)
                send_telegram_alert(f"🛑 *Hard Stop-Loss Triggered!*\nClosed position on {symbol} at premium {current_p} due to loss limit ({profit_pct:.2f}%).")
                is_position_active = False
                expiry_day_shift += 1
                update_dashboard("Position Closed (Hard Stop-Loss Hit)", btc_p, False, None, 0, current_p, vwap_p, profit_pct)
                break

            # Exit Condition 3: Candle closes above VWAP
            if current_p > vwap_p:
                print(f"⚠️ Candle closed above VWAP! Exiting option position...", flush=True)
                exchange.create_order(symbol, 'market', 'buy', TARGET_LOTS)
                send_telegram_alert(f"⚠️ *VWAP Exit Triggered!*\nClosed position on {symbol} as premium crossed above VWAP.")
                is_position_active = False
                expiry_day_shift += 1
                update_dashboard("Position Closed (VWAP Crossed)", btc_p, False, None, 0, current_p, vwap_p, profit_pct)
                break
                
            time.sleep(15)
        except Exception as e:
            print(f"Position Management Error: {e}", flush=True)
            time.sleep(15)

def option_bot_loop():
    global is_position_active, current_position_symbol
    print("🤖 BTC Strict 1st OTM Option Selling Bot Initialized...", flush=True)
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
            
            try:
                ticker = exchange.fetch_ticker('BTC/USD:BTC')
                current_btc = ticker['last'] if ticker else 0
            except:
                current_btc = 0

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
                update_dashboard("Square-off Time (4:44 PM)", current_btc, False)
                time.sleep(60)
                continue
                
            # 2. No-Trade Zone: 4:45 PM to 6:29 PM IST
            if dtime(16, 45) <= t < dtime(18, 30):
                print("⏳ No-Trade Zone active (4:45 PM - 6:29 PM). Sleeping...", flush=True)
                update_dashboard("No-Trade Zone (4:45 PM - 6:29 PM)", current_btc, False)
                time.sleep(60)
                continue
                
            # 3. Entry Window starting from 6:30 PM IST onwards
            if not is_position_active and (t.hour > 18 or (t.hour == 18 and t.minute >= 30)):
                if t.hour < 23:
                    print("🔍 Scanning Strict 1st OTM options chain...", flush=True)
                    update_dashboard("Scanning Strict 1st OTM Options", current_btc, False)
                    
                    symbol, entry_price, btc_p = find_strict_first_otm_option(exchange)
                    if symbol and entry_price:
                        print(f"🚀 Placing Sell Order for {TARGET_LOTS} lots of {symbol} at 125x leverage...", flush=True)
                        response = exchange.create_order(symbol, 'market', 'sell', TARGET_LOTS)
                        
                        is_position_active = True
                        current_position_symbol = symbol
                        
                        send_telegram_alert(f"🚨 *Strict 1st OTM Option Sell Executed!*\nSymbol: {symbol}\nLots: {TARGET_LOTS}\nLeverage: {LEVERAGE}x\nEntry Premium: {entry_price}")
                        update_dashboard("Trade Executed Successfully", btc_p, True, symbol, entry_price, entry_price, 0, 0)
                        
                        threading.Thread(target=manage_option_position, args=(exchange, symbol, entry_price), daemon=True).start()
                    else:
                        print("⏳ No suitable 1st OTM option found below VWAP. Retrying in 60s...", flush=True)
                        update_dashboard("Waiting for VWAP condition on 1st OTM", current_btc, False)
            else:
                if not is_position_active:
                    update_dashboard("Waiting for 6:30 PM IST Entry Window", current_btc, False)
                    
            time.sleep(60)
            
    except Exception as e:
        print(f"Fatal Option Bot Error: {e}", flush=True)
        update_dashboard(f"Error: {str(e)}", 0, False)

@app.before_request
def start_bot_once():
    global bot_started
    if not bot_started:
        bot_started = True
        threading.Thread(target=option_bot_loop, daemon=True).start()

@app.route('/')
def home():
    return jsonify(dashboard_status)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
