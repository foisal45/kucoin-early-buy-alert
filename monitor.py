import os
import time
import json
import requests
import websocket
from collections import defaultdict, deque

API = "https://api.kucoin.com"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ==============================
# SETTINGS
# ==============================

# Minimum first move from today's starting/accumulation area
MIN_PUMP = 15.0

# Retest must come close to original buying zone
ZONE_DISTANCE = 6.0

# Buying must be meaningfully larger than selling
BUY_SELL_RATIO = 1.30

# Minimum meaningful market buy
MIN_BUY_USD = 250

# Need repeated buying
MIN_BUYS = 3

# Same coin alert cooldown
COOLDOWN = 6 * 60 * 60

# Ignore new listings
MIN_LISTED_DAYS = 30


# ==============================
# DATA
# ==============================

trades = defaultdict(
    lambda: deque(maxlen=1000)
)

last_alert = {}

today_structure = {}


# ==============================
# TELEGRAM
# ==============================

def send_telegram(message):

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

        print("Telegram:", r.status_code)

        return r.status_code == 200

    except Exception as e:
        print("Telegram error:", e)
        return False


# ==============================
# KUCOIN SYMBOLS
# ==============================

def get_symbols():

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

        print("Symbol error:", e)
        return []

    now = int(
        time.time() * 1000
    )

    result = []

    for x in data:

        try:

            if x.get("quoteCurrency") != "USDT":
                continue

            if not x.get("enableTrading"):
                continue

            listed = x.get("listAt")

            if listed:

                age_days = (
                    now - int(listed)
                ) / 86400000

                if age_days < MIN_LISTED_DAYS:
                    continue

            result.append(
                x["symbol"]
            )

        except Exception:
            continue

    print(
        "USDT coins:",
        len(result)
    )

    return result


# ==============================
# 15M CANDLES
# ==============================

def get_candles(symbol):

    end = int(time.time())

    # Today's data only
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

        if r.status_code != 200:
            return []

        raw = r.json().get(
            "data",
            []
        )

    except Exception:
        return []

    candles = []

    for c in raw:

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

    candles.sort(
        key=lambda x: x["time"]
    )

    return candles


# ==============================
# BUILD TODAY'S FIRST BUY ZONE
# ==============================

def build_structure(symbol):

    cs = get_candles(symbol)

    if len(cs) < 12:
        return None

    # Ignore current unfinished candle
    completed = cs[:-1]

    if len(completed) < 10:
        return None

    # Only today's candles
    day = completed[-96:]

    if len(day) < 10:
        return None

    # Find the strongest volume candle
    # before the first major move
    volume_sorted = sorted(
        day,
        key=lambda x: x["volume"],
        reverse=True
    )

    candidates = volume_sorted[:8]

    best = None

    for c in candidates:

        price = c["close"]

        if price <= 0:
            continue

        # Volume average before this candle
        index = day.index(c)

        if index < 3:
            continue

        previous = day[
            max(0, index - 5):index
        ]

        avg_volume = sum(
            x["volume"]
            for x in previous
        ) / len(previous)

        if avg_volume <= 0:
            continue

        volume_ratio = (
            c["volume"]
            / avg_volume
        )

        # We want unusual activity
        if volume_ratio < 1.5:
            continue

        if best is None:
            best = (
                c,
                volume_ratio
            )

    if not best:
        return None

    zone_candle = best[0]
    volume_ratio = best[1]

    zone_low = zone_candle["low"]
    zone_high = zone_candle["high"]

    # Search price movement after zone
    zone_index = day.index(
        zone_candle
    )

    after = day[
        zone_index + 1:
    ]

    if len(after) < 3:
        return None

    peak = max(
        x["high"]
        for x in after
    )

    # Pump from zone
    pump = (
        (peak - zone_high)
        / zone_high
    ) * 100

    if pump < MIN_PUMP:
        return None

    return {
        "zone_low": zone_low,
        "zone_high": zone_high,
        "peak": peak,
        "pump": pump,
        "volume_ratio": volume_ratio,
        "zone_time": zone_candle["time"]
    }


# ==============================
# CHECK RETEST
# ==============================

def check_retest(symbol):

    structure = today_structure.get(
        symbol
    )

    if not structure:
        return None

    cs = get_candles(symbol)

    if len(cs) < 5:
        return None

    current = cs[-2]

    price = current["close"]

    zone_low = structure[
        "zone_low"
    ]

    zone_high = structure[
        "zone_high"
    ]

    # Expanded retest zone
    lower = zone_low * (
        1 - ZONE_DISTANCE / 100
    )

    upper = zone_high * (
        1 + ZONE_DISTANCE / 100
    )

    # Price must return close to original
    # buying zone
    if price < lower:
        return None

    if price > upper:
        return None

    # Current candle should show buying
    if current["close"] <= current["open"]:
        return None

    # Compare current volume
    previous = cs[-7:-2]

    if len(previous) < 3:
        return None

    avg_volume = sum(
        x["volume"]
        for x in previous
    ) / len(previous)

    if avg_volume <= 0:
        return None

    volume_ratio = (
        current["volume"]
        / avg_volume
    )

    # Fresh volume must return
    if volume_ratio < 1.20:
        return None

    return {
        "price": price,
        "volume_ratio": volume_ratio
    }


# ==============================
# BUYING FOOTPRINT
# ==============================

def buying_again(symbol):

    now = time.time()

    recent = [
        x
        for x in trades[symbol]
        if now - x["time"] <= 180
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

    big_buys = [
        x
        for x in buys
        if x["usd"] >= MIN_BUY_USD
    ]

    if len(big_buys) < MIN_BUYS:
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
        ratio = (
            buy_usd / sell_usd
        )
    else:
        ratio = 999

    if ratio < BUY_SELL_RATIO:
        return None

    return {
        "buys": len(big_buys),
        "ratio": ratio
    }


# ==============================
# 1H CONFIRMATION
# ==============================

def check_1h(symbol):

    end = int(time.time())

    start = end - (
        48 * 60 * 60
    )

    params = {
        "symbol": symbol,
        "type": "1hour",
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
        return False

    if len(raw) < 8:
        return False

    cs = []

    for c in raw:

        try:

            cs.append({
                "open": float(c[1]),
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4])
            })

        except Exception:
            continue

    if len(cs) < 6:
        return False

    cs = cs[:-1]

    recent = cs[-4:]

    # Recent 1H structure should not be
    # making continuous lower lows
    lows = [
        x["low"]
        for x in recent
    ]

    if lows[-1] < min(
        lows[:-1]
    ) * 0.97:
        return False

    # Last completed 1H candle
    last = recent[-1]

    # Avoid strongly bearish candle
    if last["close"] < last["open"]:

        body = (
            last["open"]
            - last["close"]
        )

        range_size = (
            last["high"]
            - last["low"]
        )

        if range_size > 0:

            if body / range_size > 0.65:
                return False

    return True


# ==============================
# FINAL SIGNAL
# ==============================

def check_signal(symbol):

    structure = today_structure.get(
        symbol
    )

    if not structure:
        return

    retest = check_retest(
        symbol
    )

    if not retest:
        return

    # 1H must be healthy
    if not check_1h(symbol):
        return

    # Fresh buying must return
    buying = buying_again(
        symbol
    )

    if not buying:
        return

    now = time.time()

    if symbol in last_alert:

        if (
            now - last_alert[symbol]
            < COOLDOWN
        ):
            return

    message = (
        "🚨 SECOND MOVE SETUP\n\n"
        "🪙 " + symbol + "\n"
        "📍 Buy Zone: "
        + format(
            structure["zone_low"],
            ".8g"
        )
        + " - "
        + format(
            structure["zone_high"],
            ".8g"
        )
        + "\n"
        "🚀 First Move: +"
        + format(
            structure["pump"],
            ".1f"
        )
        + "%\n"
        "🔄 Same Zone Retest\n"
        "🔥 Fresh Buying: "
        + str(
            buying["buys"]
        )
        + " buys\n"
        "💵 Buy/Sell: "
        + format(
            buying["ratio"],
            ".1f"
        )
        + "x\n"
        "📊 1H: Bullish/Healthy"
    )

    print(
        "\n" + message + "\n"
    )

    if send_telegram(
        message
    ):

        last_alert[symbol] = now


# ==============================
# WEBSOCKET
# ==============================

def get_ws():

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


def run_websocket(symbol_list):

    while True:

        info = get_ws()

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
                    time.time() * 1000
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

            chunk_size = 50

            for i in range(
                0,
                len(symbol_list),
                chunk_size
            ):

                group = symbol_list[
                    i:i + chunk_size
                ]

                topic = (
                    "/market/match:"
                    + ",".join(group)
                )

                msg = {
                    "id": str(
                        int(
                            time.time()
                            * 1000
                        )
                    ),
                    "type": "subscribe",
                    "topic": topic,
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

                size = d.get(
                    "size"
                )

                if not all([
                    symbol,
                    side,
                    price,
                    size
                ]):
                    return

                price = float(price)
                size = float(size)

                usd = price * size

                trades[symbol].append({
                    "time": time.time(),
                    "side": side.lower(),
                    "usd": usd
                })

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

                    check_signal(
                        symbol
                    )

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
                "WebSocket run error:",
                e
            )

        print(
            "Reconnect in 10 seconds..."
        )

        time.sleep(10)


# ==============================
# STRUCTURE REFRESH
# ==============================

def structure_loop(symbols):

    while True:

        print(
            "Updating today's structures..."
        )

        for symbol in symbols:

            try:

                s = build_structure(
                    symbol
                )

                if s:

                    today_structure[
                        symbol
                    ] = s

            except Exception:
                pass

            time.sleep(0.10)

        print(
            "Structure update complete."
        )

        # Refresh every 15 minutes
        time.sleep(900)


# ==============================
# MAIN
# ==============================

def main():

    print(
        "======================================"
    )

    print(
        " KUCOIN SECOND MOVE SETUP BOT"
    )

    print(
        "======================================"
    )

    if not TOKEN or not CHAT_ID:

        print(
            "Telegram secrets missing"
        )

        return

    symbols = get_symbols()

    if not symbols:
        return

    # Build structures
    print(
        "Building today's buying zones..."
    )

    for symbol in symbols:

        try:

            s = build_structure(
                symbol
            )

            if s:

                today_structure[
                    symbol
                ] = s

        except Exception:
            pass

        time.sleep(0.10)

    print(
        "Buying zones:",
        len(today_structure)
    )

    # Refresh structure in background
    thread = threading.Thread(
        target=structure_loop,
        args=(symbols,),
        daemon=True
    )

    thread.start()

    # Live buying monitor
    run_websocket(
        symbols
    )


if __name__ == "__main__":
    main()
