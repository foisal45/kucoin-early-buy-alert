import os
import json
import time
import requests
import websocket

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

KUCOIN_API = "https://api.kucoin.com"

MIN_BUY_USD = 400
WINDOW_SECONDS = 120
MAX_ABOVE_LOW = 15

# Same coin alert cooldown
ALERT_COOLDOWN = 600

# 24h stats cache
STATS_CACHE_SECONDS = 15

# GitHub Actions:
# Run for about 8.5 minutes, then close cleanly.
MAX_RUNTIME_SECONDS = 510

# ============================================================
# MEMORY
# ============================================================

buy_history = {}
last_alert = {}
stats_cache = {}

session = requests.Session()

start_time = time.time()
ws_connection = None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("ERROR: Telegram credentials missing.")
        return False

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

        if response.ok:
            print("Telegram alert sent.")
            return True

        print("Telegram error:", response.text)
        return False

    except Exception as e:
        print("Telegram exception:", e)
        return False


# ============================================================
# GET SYMBOLS
# ============================================================

def get_symbols():
    url = f"{KUCOIN_API}/api/v2/symbols"

    try:
        response = session.get(url, timeout=15)
        response.raise_for_status()

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

        symbols.sort()

        print(f"Found {len(symbols)} USDT pairs.")

        return symbols

    except Exception as e:
        print("Symbol API error:", e)
        return []


# ============================================================
# 24H STATS
# ============================================================

def get_24h_stats(symbol):
    now = time.time()

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

        response.raise_for_status()

        data = response.json().get("data")

        if not data:
            return None

        price = float(data.get("last", 0))
        low = float(data.get("lowPrice", 0))

        if price <= 0 or low <= 0:
            return None

        result = {
            "price": price,
            "low": low
        }

        stats_cache[symbol] = {
            "time": now,
            "data": result
        }

        return result

    except Exception as e:
        print(f"Stats error [{symbol}]: {e}")
        return None


# ============================================================
# PROCESS TRADE
# ============================================================

def process_trade(symbol, price, size, side):

    if side.lower() != "buy":
        return

    usd_value = price * size

    # Individual BUY must be at least $400
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

    # Only alert while price is <= 15% above 24h low
    if above_low > MAX_ABOVE_LOW:
        return

    now = time.time()

    if symbol not in buy_history:
        buy_history[symbol] = []

    buy_history[symbol].append({
        "time": now,
        "usd": usd_value
    })

    # Keep only buys from last 120 seconds
    buy_history[symbol] = [
        item
        for item in buy_history[symbol]
        if now - item["time"] <= WINDOW_SECONDS
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
        item["usd"]
        for item in recent_buys
    )

    largest_buy = max(
        item["usd"]
        for item in recent_buys
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

    print("\n" + message + "\n")

    if send_telegram(message):
        last_alert[symbol] = now
        buy_history[symbol] = []


# ============================================================
# WEBSOCKET MESSAGE
# ============================================================

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

        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        side = str(trade.get("side", "")).lower()

        if price <= 0 or size <= 0:
            return

        process_trade(
            symbol,
            price,
            size,
            side
        )

    except Exception as e:
        print("Message error:", e)


# ============================================================
# WEBSOCKET ERROR
# ============================================================

def on_error(ws, error):
    print("WebSocket error:", error)


# ============================================================
# WEBSOCKET CLOSE
# ============================================================

def on_close(ws, close_status_code, close_msg):
    print(
        "WebSocket closed:",
        close_status_code,
        close_msg
    )


# ============================================================
# GET WEBSOCKET TOKEN
# ============================================================

def get_ws_token():

    url = f"{KUCOIN_API}/api/v1/bullet-public"

    response = session.post(
        url,
        timeout=15
    )

    response.raise_for_status()

    result = response.json()

    data = result["data"]

    return (
        data["token"],
        data["instanceServers"][0]
    )


# ============================================================
# START WEBSOCKET
# ============================================================

def run_websocket():

    global ws_connection

    symbols = get_symbols()

    if not symbols:
        print("No symbols found.")
        return

    token, server = get_ws_token()

    ws_url = (
        f"{server['endpoint']}?"
        f"token={token}"
        f"&connectId=earlybuy"
    )

    print("Connecting to KuCoin WebSocket...")

    ws_connection = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    def on_open(ws):

        print("WebSocket connected.")

        # Subscribe one symbol at a time.
        # This is slower but simple and stable.
        for symbol in symbols:

            # Stop if GitHub Actions runtime is almost finished
            if time.time() - start_time >= MAX_RUNTIME_SECONDS:
                print("Maximum runtime reached.")
                ws.close()
                return

            subscribe = {
                "id": str(
                    int(time.time() * 1000)
                ),
                "type": "subscribe",
                "topic": f"/market/match:{symbol}",
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
                    f"Subscribe error [{symbol}]:",
                    e
                )

        print(
            f"Subscribed to {len(symbols)} symbols."
        )

    ws_connection.on_open = on_open

    # Keep connection alive
    ws_connection.run_forever(
        ping_interval=20,
        ping_timeout=10
    )


# ============================================================
# MAIN
# ============================================================

def main():

    global start_time

    start_time = time.time()

    print("==============================")
    print(" KuCoin Early Buy Detector")
    print("==============================")

    print(
        f"Minimum BUY: ${MIN_BUY_USD}"
    )

    print(
        f"Repeated BUY window: "
        f"{WINDOW_SECONDS}s"
    )

    print(
        f"Maximum above 24h low: "
        f"{MAX_ABOVE_LOW}%"
    )

    print(
        f"Maximum runtime: "
        f"{MAX_RUNTIME_SECONDS}s"
    )

    try:
        run_websocket()

    except Exception as e:
        print("WebSocket error:", e)

    print("Detector finished.")


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
