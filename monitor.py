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

API = "https://api.kucoin.com"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Buying footprint
MIN_BUY_USD = 250
WINDOW = 180
MIN_BUYS = 3
BUY_SELL_RATIO = 1.30

# Early zone
MAX_FROM_LOW = 20.0
MIN_FIRST_MOVE = 15.0
MIN_RETRACE = 8.0

# Avoid very new coins
MIN_LISTED_DAYS = 30

# Same coin cooldown
COOLDOWN = 6 * 60 * 60

# =========================================================
# DATA
# =========================================================

trades = defaultdict(
    lambda: deque(maxlen=500)
)

last_alert = {}

lock = threading.Lock()


# =========================================================
# TELEGRAM
# =========================================================

def telegram(message):

    if not TOKEN or not CHAT_ID:
        print("Telegram secrets missing")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TOKEN
        + "/sendMessage"
    )

    try:

        r = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=15
        )

        print(
            "Telegram:",
            r.status_code
        )

        return r.status_code == 200

    except Exception as e:

        print(
            "Telegram error:",
            e
        )

        return False


# =========================================================
# SYMBOLS
# =========================================================

def symbols():

    try:

        r = requests.get(
            API + "/api/v2/symbols",
            timeout=20
        )

        data = r.json().get(
            "data",
            []
        )

    except Exception as e:

        print(
            "Symbol error:",
            e
        )

        return []

    now = int(
        time.time() * 1000
    )

    result = []

    for x in data:

        try:

            if x.get(
                "quoteCurrency"
            ) != "USDT":
                continue

            if not x.get(
                "enableTrading"
            ):
                continue

            listed = x.get(
                "listAt"
            )

            if listed:

                age = (
                    now - int(listed)
                ) / 86400000

                if age < MIN_LISTED_DAYS:
                    continue

            result.append(
                x["symbol"]
            )

        except Exception:
            pass

    print(
        "USDT coins:",
        len(result)
    )

    return result


# =========================================================
# 24H MARKET DATA
# =========================================================

def market():

    try:

        r = requests.get(
            API + "/api/v1/market/allTickers",
            timeout=20
        )

        items = r.json().get(
            "data",
            {}
        ).get(
            "ticker",
            []
        )

    except Exception as e:

        print(
            "Market error:",
            e
        )

        return {}

    result = {}

    for x in items:

        symbol = x.get(
            "symbol"
        )

        if not symbol:
            continue

        if not symbol.endswith(
            "-USDT"
        ):
            continue

        try:

            result[symbol] = {
                "price": float(
                    x["last"]
                ),
                "low": float(
                    x["low"]
                ),
                "high": float(
                    x["high"]
                ),
                "change": float(
                    x["changeRate"]
                ) * 100,
                "volume": float(
                    x["volValue"]
                )
            }

        except Exception:
            pass

    return result


# =========================================================
# 15M CANDLES
# =========================================================

def candles(symbol):

    end = int(
        time.time()
    )

    start = end - 86400

    params = {
        "symbol": symbol,
        "type": "15min",
        "startAt": start,
        "endAt": end
    }

    try:

        r = requests.get(
            API + "/api/v1/market/candles",
            params=params,
            timeout=15
        )

        raw = r.json().get(
            "data",
            []
        )

    except Exception:
        return []

    result = []

    for c in raw:

        try:

            result.append({
                "time": int(c[0]),
                "open": float(c[1]),
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4]),
                "volume": float(c[5])
            })

        except Exception:
            pass

    result.sort(
        key=lambda x: x["time"]
    )

    return result


# =========================================================
# RETEST SETUP
# =========================================================

def setup(symbol):

    cs = candles(symbol)

    if len(cs) < 25:
        return None

    # Ignore current unfinished candle
    cs = cs[:-1]

    low = min(
        x["low"]
        for x in cs
    )

    peak_candle = max(
        cs[:-2],
        key=lambda x: x["high"]
    )

    peak = peak_candle["high"]

    current = cs[-1]

    price = current["close"]

    if low <= 0:
        return None

    # First move
    first_move = (
        (peak - low)
        / low
    ) * 100

    if first_move < MIN_FIRST_MOVE:
        return None

    # Current distance from low
    from_low = (
        (price - low)
        / low
    ) * 100

    if from_low < 0:
        return None

    if from_low > MAX_FROM_LOW:
        return None

    # Retracement
    retrace = (
        (peak - price)
        / peak
    ) * 100

    if retrace < MIN_RETRACE:
        return None

    return {
        "price": price,
        "low": low,
        "first": first_move,
        "retrace": retrace
    }


# =========================================================
# BUYING FOOTPRINT
# =========================================================

def buying(symbol):

    now = time.time()

    with lock:

        recent = [
            x
            for x in trades[symbol]
            if now - x["time"] <= WINDOW
        ]

    buys = [
        x
        for x in recent
        if x["side"] == "buy"
    ]

    sells = [
        x
        for x in recent
        if x["side"] == "sell"
    ]

    if len(buys) < MIN_BUYS:
        return None

    buy_usd = sum(
        x["usd"]
        for x in buys
    )

    sell_usd = sum(
        x["usd"]
        for x in sells
    )

    if sell_usd > 0:
        ratio = buy_usd / sell_usd
    else:
        ratio = 999

    if ratio < BUY_SELL_RATIO:
        return None

    big = [
        x
        for x in buys
        if x["usd"] >= MIN_BUY_USD
    ]

    if len(big) < 1:
        return None

    return {
        "buys": len(buys),
        "big": len(big),
        "ratio": ratio
    }


# =========================================================
# SIGNAL
# =========================================================

def signal(symbol):

    m = market().get(
        symbol
    )

    if not m:
        return

    s = setup(symbol)

    if not s:
        return

    b = buying(symbol)

    if not b:
        return

    now = time.time()

    if symbol in last_alert:

        if (
            now - last_alert[symbol]
            < COOLDOWN
        ):
            return

    message = (
        "🚨 EARLY BUY SETUP\n\n"
        "🪙 " + symbol + "\n"
        "💰 Now: "
        + format(m["price"], ".8g")
        + "\n"
        "📍 24H Low: "
        + format(s["low"], ".8g")
        + "\n"
        "📈 First Move: +"
        + format(s["first"], ".1f")
        + "%\n"
        "🔄 Retrace: -"
        + format(s["retrace"], ".1f")
        + "%\n"
        "🔥 Repeated Buys: "
        + str(b["buys"])
        + "\n"
        "💵 Buy/Sell: "
        + format(b["ratio"], ".1f")
        + "x"
    )

    print(
        "\n" + message + "\n"
    )

    if telegram(message):

        last_alert[symbol] = now


# =========================================================
# WEBSOCKET TOKEN
# =========================================================

def ws_token():

    try:

        r = requests.post(
            API + "/api/v1/bullet-public",
            timeout=20
        )

        data = r.json()["data"]

        endpoint = data[
            "instanceServers"
        ][0]["endpoint"]

        token = data["token"]

        return endpoint, token

    except Exception as e:

        print(
            "WS token error:",
            e
        )

        return None


# =========================================================
# WEBSOCKET
# =========================================================

def run_ws(symbol_list):

    while True:

        info = ws_token()

        if not info:

            time.sleep(10)
            continue

        endpoint, token = info

        url = (
            endpoint
            + "?token="
            + token
            + "&connectId="
            + str(
                int(
                    time.time()
                    * 1000
                )
            )
        )

        print(
            "Connecting WebSocket..."
        )

        def on_open(ws):

            print(
                "WebSocket connected."
            )

            # Subscribe in groups
            size = 50

            for i in range(
                0,
                len(symbol_list),
                size
            ):

                group = symbol_list[
                    i:i + size
                ]

                msg = {
                    "id": str(
                        int(
                            time.time()
                            * 1000
                        )
                    ),
                    "type": "subscribe",
                    "topic": (
                        "/market/match:"
                        + ",".join(group)
                    ),
                    "privateChannel": False,
                    "response": True
                }

                ws.send(
                    json.dumps(msg)
                )

            print(
                "Subscribed:",
                len(symbol_list)
            )

        def on_message(
            ws,
            message
        ):

            try:

                data = json.loads(
                    message
                )

                if data.get(
                    "type"
                ) != "message":
                    return

                d = data.get(
                    "data",
                    {}
                )

                symbol = d.get(
                    "symbol"
                )

                side = d.get(
                    "side"
                )

                price = d.get(
                    "price"
                )

                amount = d.get(
                    "size"
                )

                if not all([
                    symbol,
                    side,
                    price,
                    amount
                ]):
                    return

                price = float(price)
                amount = float(amount)

                usd = (
                    price * amount
                )

                with lock:

                    trades[symbol].append({
                        "time": time.time(),
                        "side": side.lower(),
                        "usd": usd
                    })

                # Only analyse meaningful buy
                if (
                    side.lower() == "buy"
                    and usd >= MIN_BUY_USD
                ):

                    print(
                        "BUY",
                        symbol,
                        "$",
                        round(usd, 2)
                    )

                    signal(symbol)

            except Exception as e:

                print(
                    "Message error:",
                    e
                )

        def on_error(
            ws,
            error
        ):

            print(
                "WebSocket error:",
                error
            )

        def on_close(
            ws,
            code,
            msg
        ):

            print(
                "WebSocket closed:",
                code,
                msg
            )

        try:

            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            ws.run_forever(
                ping_interval=15,
                ping_timeout=10
            )

        except Exception as e:

            print(
                "WebSocket error:",
                e
            )

        print(
            "Reconnect in 10 seconds..."
        )

        time.sleep(10)


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "======================================"
    )

    print(
        " KUCOIN EARLY BUY FOOTPRINT BOT"
    )

    print(
        "======================================"
    )

    if not TOKEN or not CHAT_ID:

        print(
            "Telegram secrets missing"
        )

        return

    symbol_list = symbols()

    if not symbol_list:

        print(
            "No symbols found"
        )

        return

    print(
        "Starting live scanner..."
    )

    run_ws(
        symbol_list
    )


if __name__ == "__main__":
    main()
