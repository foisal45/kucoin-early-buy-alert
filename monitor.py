import os
import time
import json
import threading
from collections import defaultdict, deque

import requests
import websocket


# =========================================================
# CONFIG
# =========================================================

KUCOIN_API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Setup rules
MAX_ABOVE_24H_LOW = 15.0       # Current price must be <= +15% from 24H low
MIN_FIRST_MOVE = 20.0           # Coin must first move at least +20%
MIN_RETRACE = 15.0              # At least 15% retrace from the pump high

# Large buy activity
MIN_BUY_USD = 400
BUY_WINDOW_SECONDS = 120
MIN_BUY_COUNT = 2

# Do not alert newly listed coins
MIN_LISTED_DAYS = 30

# Avoid repeated alerts
ALERT_COOLDOWN_SECONDS = 6 * 60 * 60

# Poll 15M candle data
SCAN_INTERVAL = 60


# =========================================================
# GLOBAL STATE
# =========================================================

symbols = []
symbol_info = {}

# symbol -> deque of (timestamp, usd_value)
buy_activity = defaultdict(deque)

# symbol -> last alert timestamp
last_alert = {}

# symbols already triggered during current process
triggered = set()

lock = threading.Lock()


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:
        r = requests.post(url, json=payload, timeout=15)
        print("Telegram:", r.status_code)
    except Exception as e:
        print("Telegram error:", e)


# =========================================================
# KUCOIN SYMBOLS
# =========================================================

def load_symbols():
    global symbols, symbol_info

    url = f"{KUCOIN_API}/api/v2/symbols"

    r = requests.get(url, timeout=20)
    r.raise_for_status()

    data = r.json()["data"]

    now_ms = int(time.time() * 1000)

    result = []

    for item in data:
        try:
            symbol = item["symbol"]

            # Only USDT spot pairs
            if item.get("quoteCurrency") != "USDT":
                continue

            # Trading must be enabled
            if not item.get("enableTrading"):
                continue

            # -------------------------------------------------
            # Exclude newly listed coins
            # -------------------------------------------------
            listed_at = item.get("listAt")

            if listed_at:
                age_days = (now_ms - int(listed_at)) / 1000 / 86400

                if age_days < MIN_LISTED_DAYS:
                    continue

            result.append(symbol)

            symbol_info[symbol] = item

        except Exception:
            continue

    symbols = result

    print(f"Loaded existing USDT coins: {len(symbols)}")


# =========================================================
# 15 MIN CANDLES
# =========================================================

def get_15m_candles(symbol):
    """
    KuCoin candle:
    [time, open, close, high, low, volume, turnover]
    """

    end = int(time.time())
    start = end - (24 * 60 * 60)

    url = f"{KUCOIN_API}/api/v1/market/candles"

    params = {
        "symbol": symbol,
        "type": "15min",
        "startAt": start,
        "endAt": end
    }

    try:
        r = requests.get(url, params=params, timeout=15)

        if r.status_code != 200:
            return []

        data = r.json().get("data", [])

        candles = []

        for c in data:
            try:
                candles.append({
                    "time": int(c[0]),
                    "open": float(c[1]),
                    "close": float(c[2]),
                    "high": float(c[3]),
                    "low": float(c[4]),
                    "volume": float(c[5])
                })
            except Exception:
                continue

        candles.sort(key=lambda x: x["time"])

        return candles

    except Exception as e:
        print(f"Candle error {symbol}: {e}")
        return []


# =========================================================
# SETUP DETECTION
# =========================================================

def detect_setup(symbol):
    candles = get_15m_candles(symbol)

    if len(candles) < 10:
        return None

    # 24H low
    low_24h = min(c["low"] for c in candles)

    # Highest point reached during the last 24h
    peak = max(c["high"] for c in candles)

    if low_24h <= 0:
        return None

    first_move_pct = ((peak - low_24h) / low_24h) * 100

    # -------------------------------------------------
    # First move must be strong enough
    # -------------------------------------------------

    if first_move_pct < MIN_FIRST_MOVE:
        return None

    current_price = candles[-1]["close"]

    # -------------------------------------------------
    # Current price must be within +15% of 24H low
    # -------------------------------------------------

    above_low_pct = ((current_price - low_24h) / low_24h) * 100

    if above_low_pct < 0:
        above_low_pct = 0

    if above_low_pct > MAX_ABOVE_24H_LOW:
        return None

    # -------------------------------------------------
    # Must have retraced at least 15% from the peak
    # -------------------------------------------------

    if peak <= 0:
        return None

    retrace_pct = ((peak - current_price) / peak) * 100

    if retrace_pct < MIN_RETRACE:
        return None

    # -------------------------------------------------
    # Make sure the peak happened before current candle
    # -------------------------------------------------

    peak_index = max(
        range(len(candles)),
        key=lambda i: candles[i]["high"]
    )

    if peak_index >= len(candles) - 1:
        return None

    return {
        "current": current_price,
        "low": low_24h,
        "peak": peak,
        "first_move": first_move_pct,
        "above_low": above_low_pct,
        "retrace": retrace_pct
    }


# =========================================================
# BUY ACTIVITY
# =========================================================

def record_buy(symbol, usd_value):
    now = time.time()

    with lock:
        q = buy_activity[symbol]

        q.append((now, usd_value))

        # Remove old trades
        while q and now - q[0][0] > BUY_WINDOW_SECONDS:
            q.popleft()


def buy_activity_rising(symbol):
    now = time.time()

    with lock:
        q = buy_activity[symbol]

        # Clean old trades
        while q and now - q[0][0] > BUY_WINDOW_SECONDS:
            q.popleft()

        if len(q) < MIN_BUY_COUNT:
            return False

        total = sum(x[1] for x in q)

        # Multiple large buys in short period
        if total >= MIN_BUY_USD * MIN_BUY_COUNT:
            return True

        return False


# =========================================================
# WEBSOCKET
# =========================================================

def get_ws_token():
    url = f"{KUCOIN_API}/api/v1/bullet-public"

    r = requests.post(url, timeout=20)
    r.raise_for_status()

    data = r.json()["data"]

    token = data["token"]
    server = data["instanceServers"][0]

    endpoint = server["endpoint"]
    ping_interval = server.get("pingInterval", 18000)

    return token, endpoint, ping_interval


def subscribe_symbols(ws, chunk):
    """
    Classic KuCoin WebSocket supports the match topic.
    One subscription can contain multiple symbols.
    """

    topic_symbols = ",".join(chunk)

    message = {
        "id": str(int(time.time() * 1000)),
        "type": "subscribe",
        "topic": f"/market/match:{topic_symbols}",
        "response": True
    }

    ws.send(json.dumps(message))


def on_message(ws, message):
    try:
        data = json.loads(message)

        if data.get("type") != "message":
            return

        msg = data.get("data", {})

        symbol = msg.get("symbol")
        side = msg.get("side")

        if not symbol or side != "buy":
            return

        price = float(msg.get("price", 0))
        size = float(msg.get("size", 0))

        if price <= 0 or size <= 0:
            return

        usd_value = price * size

        # Only count large individual buys
        if usd_value < MIN_BUY_USD:
            return

        record_buy(symbol, usd_value)

        print(
            f"BUY {symbol} | "
            f"${usd_value:.2f}"
        )

    except Exception as e:
        print("WS message error:", e)


def on_error(ws, error):
    print("WebSocket error:", error)


def on_close(ws, close_status_code, close_msg):
    print(
        "WebSocket closed:",
        close_status_code,
        close_msg
    )


def websocket_worker():
    while True:

        try:
            token, endpoint, ping_interval = get_ws_token()

            ws_url = (
                f"{endpoint}"
                f"?token={token}"
                f"&connectId={int(time.time() * 1000)}"
            )

            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            def on_open(socket):
                print("WebSocket connected.")

                # KuCoin topic supports up to 100 symbols
                for i in range(0, len(symbols), 100):
                    chunk = symbols[i:i + 100]

                    try:
                        subscribe_symbols(socket, chunk)
                        time.sleep(0.2)
                    except Exception as e:
                        print("Subscribe error:", e)

            ws.on_open = on_open

            ws.run_forever(
                ping_interval=max(10, int(ping_interval / 1000) - 2),
                ping_timeout=10
            )

        except Exception as e:
            print("WebSocket restart error:", e)

        print("Restarting WebSocket in 10 seconds...")
        time.sleep(10)


# =========================================================
# ALERT CHECK
# =========================================================

def check_symbol(symbol):

    # Don't repeatedly process triggered coins
    if symbol in triggered:
        return

    setup = detect_setup(symbol)

    if not setup:
        return

    # Buying must be increasing
    if not buy_activity_rising(symbol):
        return

    now = time.time()

    # Cooldown
    if symbol in last_alert:
        if now - last_alert[symbol] < ALERT_COOLDOWN_SECONDS:
            return

    current = setup["current"]
    low = setup["low"]
    first_move = setup["first_move"]
    above_low = setup["above_low"]

    message = (
        "🚨 15M RETEST SETUP\n\n"
        f"🪙 {symbol}\n"
        f"📈 First Move: +{first_move:.1f}%\n\n"
        f"📍 24H Low: {low:.8g}\n"
        f"💰 Now: {current:.8g} (+{above_low:.1f}%)\n\n"
        "🔥 Buy activity rising"
    )

    print("\n" + message + "\n")

    send_telegram(message)

    last_alert[symbol] = now
    triggered.add(symbol)


# =========================================================
# MAIN SCANNER
# =========================================================

def scanner():

    while True:

        print(
            "\n=============================="
        )
        print("Scanning 15M retest setups...")
        print("==============================")

        for symbol in symbols:

            try:
                check_symbol(symbol)
            except Exception as e:
                print(
                    f"Scan error {symbol}: {e}"
                )

            # Small delay to reduce API pressure
            time.sleep(0.15)

        print(
            f"Scan completed. "
            f"Next scan in {SCAN_INTERVAL}s."
        )

        time.sleep(SCAN_INTERVAL)


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    print("===================================")
    print(" KuCoin 15M Retest Early Buy Alert ")
    print("===================================")

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(
            "ERROR: TELEGRAM_TOKEN or CHAT_ID "
            "is missing."
        )
        raise SystemExit(1)

    load_symbols()

    if not symbols:
        print("No eligible symbols found.")
        raise SystemExit(1)

    # Start real-time BUY activity listener
    ws_thread = threading.Thread(
        target=websocket_worker,
        daemon=True
    )

    ws_thread.start()

    # Start scanner
    scanner()
