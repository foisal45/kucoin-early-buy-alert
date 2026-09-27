import os
import json
import time
import threading
import websocket
import requests

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

KUCOIN_API = "https://api.kucoin.com"

MIN_BUY_USD = 400
WINDOW_SECONDS = 120
MAX_ABOVE_LOW = 15

# Same coin will not alert again within this period
ALERT_COOLDOWN = 600

# How often we refresh 24h stats for a symbol
STATS_CACHE_SECONDS = 10

# KuCoin allows up to 100 symbols per /market/match topic
BATCH_SIZE = 100

# ============================================================
# MEMORY
# ============================================================

buy_history = {}
last_alert = {}
stats_cache = {}

session = requests.Session()

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("ERROR: Telegram credentials are missing.")
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
# GET ALL USDT SYMBOLS
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

        print(f"Found {len(symbols)} USDT trading pairs.")

        return symbols

    except Exception as e:
        print("Symbol API error:", e)
        return []


# ============================================================
# GET 24H STATS
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

        last_price = float(data.get("last", 0))
        low_price = float(data.get("lowPrice", 0))

        if last_price <= 0 or low_price <= 0:
            return None

        result = {
            "price": last_price,
            "low": low_price
        }

        stats_cache[symbol] = {
            "time": now,
            "data": result
        }

        return result

    except Exception as e:
        print(f"Stats error [{symbol}]:", e)
        return None


# ============================================================
# PROCESS BUY TRADE
# ============================================================

def process_trade(symbol, price, size, side):
    if side.lower() != "buy":
        return

    usd_value = price * size

    # Individual large buy threshold
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

    # Ignore coins more than +15% above 24h low
    if above_low > MAX_ABOVE_LOW:
        return

    now = time.time()

    # Create history
    if symbol not in buy_history:
        buy_history[symbol] = []

    # Add this large buy
    buy_history[symbol].append({
        "time": now,
        "usd": usd_value
    })

    # Remove old buys outside the 120-second window
    buy_history[symbol] = [
        item
        for item in buy_history[symbol]
        if now - item["time"] <= WINDOW_SECONDS
    ]

    recent_buys = buy_history[symbol]

    # Need at least 2 large buys in the window
    if len(recent_buys) < 2:
        return

    # Prevent repeated alerts
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

    sent = send_telegram(message)

    if sent:
        last_alert[symbol] = now

        # Reset after successful alert
        buy_history[symbol] = []


# ============================================================
# WEBSOCKET MESSAGE
# ============================================================

def on_message(ws, message):
    try:
        data = json.loads(message)

        message_type = data.get("type")

        # Ignore welcome / ack / other messages
        if message_type != "message":
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
            symbol=symbol,
            price=price,
            size=size,
            side=side
        )

    except Exception as e:
        print("Message processing error:", e)


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
        "WebSocket closed.",
        "Code:", close_status_code,
        "Message:", close_msg
    )


# ============================================================
# GET KUCOIN PUBLIC WEBSOCKET TOKEN
# ============================================================

def get_ws_token():
    url = f"{KUCOIN_API}/api/v1/bullet-public"

    response = session.post(
        url,
        timeout=15
    )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != "200000":
        raise RuntimeError(
            f"KuCoin token error: {result}"
        )

    data = result["data"]

    token = data["token"]
    server = data["instanceServers"][0]

    return token, server


# ============================================================
# SUBSCRIBE TO SYMBOL BATCH
# ============================================================

def subscribe_batch(ws, symbols):
    topic_symbols = ",".join(symbols)

    subscribe = {
        "id": str(int(time.time() * 1000)),
        "type": "subscribe",
        "topic": f"/market/match:{topic_symbols}",
        "privateChannel": False,
        "response": True
    }

    ws.send(json.dumps(subscribe))

    print(
        f"Subscribed to {len(symbols)} symbols."
    )


# ============================================================
# START ONE WEBSOCKET CONNECTION
# ============================================================

def run_websocket():
    symbols = get_symbols()

    if not symbols:
        print("No USDT symbols found.")
        return

    token, server = get_ws_token()

    endpoint = server["endpoint"]
    ping_interval_ms = server.get(
        "pingInterval",
        18000
    )

    # KuCoin recommends client ping based on pingInterval.
    ping_seconds = max(
        5,
        int(ping_interval_ms / 1000) - 2
    )

    ws_url = (
        f"{endpoint}"
        f"?token={token}"
        f"&connectId=earlybuy"
    )

    print("Connecting to KuCoin WebSocket...")
    print(f"Ping interval: {ping_seconds}s")

    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    def on_open(ws_connection):
        print("WebSocket connected.")

        # Subscribe in batches.
        # KuCoin Classic Spot allows up to 100 symbols
        # in the /market/match topic.
        for start in range(
            0,
            len(symbols),
            BATCH_SIZE
        ):
            batch = symbols[
                start:start + BATCH_SIZE
            ]

            try:
                subscribe_batch(
                    ws_connection,
                    batch
                )

                time.sleep(0.2)

            except Exception as e:
                print(
                    "Subscription error:",
                    e
                )

        print("All subscriptions sent.")

    ws.on_open = on_open

    # Keep connection alive.
    ws.run_forever(
        ping_interval=ping_seconds,
        ping_timeout=10
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("==============================")
    print("  KuCoin Early Buy Detector")
    print("==============================")

    print(
        f"Minimum individual BUY: "
        f"${MIN_BUY_USD}"
    )

    print(
        f"Repeated BUY window: "
        f"{WINDOW_SECONDS} seconds"
    )

    print(
        f"Maximum above 24h low: "
        f"{MAX_ABOVE_LOW}%"
    )

    print(
        f"Alert cooldown: "
        f"{ALERT_COOLDOWN} seconds"
    )

    while True:
        try:
            run_websocket()

        except Exception as e:
            print(
                "Main WebSocket error:",
                e
            )

        print(
            "Reconnecting in 10 seconds..."
        )

        time.sleep(10)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
