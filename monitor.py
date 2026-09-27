import os
import json
import time
import websocket
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

KUCOIN_API = "https://api.kucoin.com"

MIN_BUY_USD = 400
WINDOW_SECONDS = 120
MAX_ABOVE_LOW = 15

buy_history = {}


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=10
    )


def get_symbols():
    url = f"{KUCOIN_API}/api/v2/symbols"
    data = requests.get(url, timeout=10).json()

    symbols = []

    for item in data.get("data", []):
        if (
            item.get("quoteCurrency") == "USDT"
            and item.get("enableTrading") is True
        ):
            symbols.append(item["symbol"])

    return symbols


def get_24h_stats(symbol):
    url = f"{KUCOIN_API}/api/v1/market/stats?symbol={symbol}"

    try:
        data = requests.get(url, timeout=5).json()["data"]

        return {
            "price": float(data["last"]),
            "low": float(data["lowPrice"])
        }

    except Exception:
        return None


def process_trade(symbol, price, size, side):
    if side != "buy":
        return

    usd_value = price * size

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

    if above_low > MAX_ABOVE_LOW:
        return

    now = time.time()

    if symbol not in buy_history:
        buy_history[symbol] = []

    buy_history[symbol].append({
        "time": now,
        "usd": usd_value
    })

    buy_history[symbol] = [
        x for x in buy_history[symbol]
        if now - x["time"] <= WINDOW_SECONDS
    ]

    recent_buys = buy_history[symbol]

    if len(recent_buys) < 2:
        return

    total_buy = sum(x["usd"] for x in recent_buys)
    largest_buy = max(x["usd"] for x in recent_buys)

    message = (
        "🚨 EARLY BUY DETECTED\n\n"
        f"🪙 {symbol}\n"
        f"💰 Current: {current_price:.8f}\n"
        f"📉 24h Low: {low:.8f}\n"
        f"📈 Above Low: +{above_low:.2f}%\n\n"
        f"🟢 Large Buys: {len(recent_buys)}\n"
        f"💵 Largest Buy: ${largest_buy:,.0f}\n"
        f"💵 Buy Volume ({WINDOW_SECONDS}s): ${total_buy:,.0f}"
    )

    send_telegram(message)

    buy_history[symbol] = []


def on_message(ws, message):
    try:
        data = json.loads(message)

        if data.get("type") != "message":
            return

        topic = data.get("topic", "")

        if not topic.startswith("/market/match:"):
            return

        symbol = topic.split(":")[1]

        trade = data.get("data", {})

        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        side = trade.get("side", "").lower()

        if price > 0 and size > 0:
            process_trade(symbol, price, size, side)

    except Exception as e:
        print("Error:", e)


def on_error(ws, error):
    print("WebSocket error:", error)


def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed")


def get_ws_token():
    url = f"{KUCOIN_API}/api/v1/bullet-public"

    response = requests.post(url, timeout=10)
    data = response.json()["data"]

    return data["token"], data["instanceServers"][0]


def main():
    symbols = get_symbols()

    token, server = get_ws_token()

    ws_url = (
        f"{server['endpoint']}?"
        f"token={token}"
        f"&connectId=earlybuy"
    )

    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    def on_open(ws):
        print(f"Monitoring {len(symbols)} USDT pairs...")

        for symbol in symbols:
            subscribe = {
                "id": str(int(time.time() * 1000)),
                "type": "subscribe",
                "topic": f"/market/match:{symbol}",
                "privateChannel": False,
                "response": True
            }

            ws.send(json.dumps(subscribe))

            time.sleep(0.02)

    ws.on_open = on_open

    ws.run_forever()


if __name__ == "__main__":
    main()
