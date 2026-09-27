import os
import json
import time
import threading
import websocket
import requests

# =========================
# CONFIG
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

KUCOIN_API = "https://api.kucoin.com"

MIN_BUY_USD = 400
WINDOW_SECONDS = 120
MAX_ABOVE_LOW = 15

# Same coin alert cooldown
ALERT_COOLDOWN = 600  # 10 minutes

# Cache 24h stats
STATS_CACHE_SECONDS = 10


# =========================
# MEMORY
# =========================

buy_history = {}
last_alert = {}
stats_cache = {}

session = requests.Session()


# =========================
# TELEGRAM
# =========================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = session.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=10
        )

        if not response.ok:
            print("Telegram error:", response.text)

    except Exception as e:
        print("Telegram exception:", e)


# =========================
# GET SYMBOLS
# =========================

def get_symbols():

    url = f"{KUCOIN_API}/api/v2/symbols"

    try:
        response = session.get(url, timeout=15)
        data = response.json()

        symbols = []

        for item in data.get("data", []):

            if (
                item.get("quoteCurrency") == "USDT"
                and item.get("enableTrading") is True
            ):
                symbol = item.get("symbol")

                if symbol:
                    symbols.append(symbol)

        print(f"Found {len(symbols)} USDT pairs.")

        return symbols

    except Exception as e:

        print("Symbol error:", e)

        return []


# =========================
# 24H STATS
# =========================

def get_24h_stats(symbol):

    now = time.time()

    # Use cache
    cached = stats_cache.get(symbol)

    if cached:
        if now - cached["time"] < STATS_CACHE_SECONDS:
            return cached["data"]

    url = f"{KUCOIN_API}/api/v1/market/stats"

    try:

        response = session.get(
            url,
            params={"symbol": symbol},
            timeout=8
        )

        data = response.json().get("data")

        if not data:
            return None

        result = {
            "price": float(data["last"]),
            "low": float(data["lowPrice"])
        }

        stats_cache[symbol] = {
            "time": now,
            "data": result
        }

        return result

    except Exception as e:

        print(f"Stats error {symbol}:", e)

        return None


# =========================
# PROCESS BUY
# =========================

def process_trade(symbol, price, size, side):

    if side != "buy":
        return

    usd_value = price * size

    # Minimum individual buy
    if usd_value < MIN_BUY_USD:
        return

    stats = get_24h_stats(symbol)

    if not stats:
        return

    current_price = stats["price"]
    low = stats["low"]

    if low <= 0:
        return

    above_low = ((current_price - low) / low) * 100

    # Only monitor within 15% above 24h low
    if above_low > MAX_ABOVE_LOW:
        return

    now = time.time()

    # Create history
    if symbol not in buy_history:
        buy_history[symbol] = []

    buy_history[symbol].append({
        "time": now,
        "usd": usd_value
    })

    # Remove old trades
    buy_history[symbol] = [
        x for x in buy_history[symbol]
        if now - x["time"] <= WINDOW_SECONDS
    ]

    recent_buys = buy_history[symbol]

    # Need at least 2 large buys
    if len(recent_buys) < 2:
        return

    # Cooldown
    if symbol in last_alert:

        if now - last_alert[symbol] < ALERT_COOLDOWN:
            return

    total_buy = sum(
        x["usd"] for x in recent_buys
    )

    largest_buy = max(
        x["usd"] for x in recent_buys
    )

    message = (
        "🚨 EARLY BUY DETECTED\n\n"
        f"🪙 {symbol}\n"
        f"💰 Current: {current_price:.8f}\n"
        f"📉 24h Low: {low:.8f}\n"
        f"📈 Above Low: +{above_low:.2f}%\n\n"
        f"🟢 Large Buys: {len(recent_buys)}\n"
        f"💵 Largest Buy: ${largest_buy:,.0f}\n"
        f"💵 Buy Volume ({WINDOW_SECONDS}s): "
        f"${total_buy:,.0f}"
    )

    send_telegram(message)

    print(message)

    last_alert[symbol] = now

    # Reset after alert
    buy_history[symbol] = []


# =========================
# WEBSOCKET MESSAGE
# =========================

def on_message(ws, message):

    try:

        data = json.loads(message)

        if data.get("type") != "message":
            return

        topic = data.get("topic", "")

        if not topic.startswith("/market/match:"):
            return

        symbol = topic.split(":", 1)[1]

        trade = data.get("data", {})

        price = float(
            trade.get("price", 0)
        )

        size = float(
            trade.get("size", 0)
        )

        side = trade.get(
            "side", ""
        ).lower()

        if price > 0 and size > 0:

            process_trade(
                symbol,
                price,
                size,
                side
            )

    except Exception as e:

        print("Message error:", e)


# =========================
# WEBSOCKET ERROR
# =========================

def on_error(ws, error):

    print("WebSocket error:", error)


# =========================
# WEBSOCKET CLOSE
# =========================

def on_close(
    ws,
    close_status_code,
    close_msg
):

    print(
        "WebSocket closed:",
        close_status_code,
        close_msg
    )


# =========================
# WEBSOCKET TOKEN
# =========================

def get_ws_token():

    url = f"{KUCOIN_API}/api/v1/bullet-public"

    response = session.post(
        url,
        timeout=15
    )

    data = response.json()["data"]

    return (
        data["token"],
        data["instanceServers"][0]
    )


# =========================
# START WEBSOCKET
# =========================

def run_websocket():

    symbols = get_symbols()

    if not symbols:

        print("No symbols found.")

        time.sleep(30)

        return

    try:

        token, server = get_ws_token()

        ws_url = (
            f"{server['endpoint']}?"
            f"token={token}"
            f"&connectId=earlybuy"
        )

        print(
            f"Connecting WebSocket "
            f"for {len(symbols)} symbols..."
        )

        ws = websocket.WebSocketApp(
            ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )

        def on_open(ws):

            print("WebSocket connected.")

            for symbol in symbols:

                subscribe = {
                    "id": str(
                        int(time.time() * 1000)
                    ),
                    "type": "subscribe",
                    "topic": (
                        f"/market/match:{symbol}"
                    ),
                    "privateChannel": False,
                    "response": True
                }

                try:

                    ws.send(
                        json.dumps(subscribe)
                    )

                    time.sleep(0.02)

                except Exception as e:

                    print(
                        "Subscribe error:",
                        symbol,
                        e
                    )

            print("All subscriptions sent.")

        ws.on_open = on_open

        ws.run_forever(
            ping_interval=20,
            ping_timeout=10
        )

    except Exception as e:

        print(
            "WebSocket connection error:",
            e
        )


# =========================
# MAIN LOOP
# =========================

def main():

    print("==============================")
    print(" KuCoin Early Buy Detector")
    print("==============================")

    print(
        f"Minimum BUY: ${MIN_BUY_USD}"
    )

    print(
        f"Window: {WINDOW
