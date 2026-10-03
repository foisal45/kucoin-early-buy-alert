import os
import time
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import websocket


# =========================================================
# CONFIG
# =========================================================

KUCOIN_API = "https://api.kucoin.com"
COINGECKO_API = "https://api.coingecko.com/api/v3"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MAX_MARKET_CAP_USD = 300_000_000
MIN_MARKET_CAP_USD = 1_000_000

MAX_ABOVE_24H_LOW = 15.0

MIN_RECOVERY_PCT = 15.0
MAX_RETRACE_PCT = 50.0

MIN_VOLUME_MULTIPLIER = 1.5

SIGNAL_COOLDOWN = 12 * 60 * 60

BD_TZ = ZoneInfo("Asia/Dhaka")


# =========================================================
# GLOBALS
# =========================================================

symbols = []
market_caps = {}

latest_price = {}
latest_24h_low = {}

deposit_status = {}

candles_15m = {}
candles_1h = {}

loaded_history = set()
last_signal = {}

lock = threading.Lock()


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials missing")
        return

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        r = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=10
        )

        print("Telegram:", r.status_code)

    except Exception as e:
        print("Telegram error:", e)


# =========================================================
# KUCOIN SYMBOLS
# =========================================================

def get_symbols():

    try:

        r = requests.get(
            f"{KUCOIN_API}/api/v2/symbols",
            timeout=20
        )

        data = r.json().get("data", [])

        result = []

        for x in data:

            if x.get("quoteCurrency") != "USDT":
                continue

            if not x.get("enableTrading"):
                continue

            symbol = x.get("symbol")

            if symbol:
                result.append(symbol)

        print("KuCoin USDT pairs:", len(result))

        return result

    except Exception as e:

        print("Symbol error:", e)
        return []


# =========================================================
# TICKERS
# =========================================================

def get_tickers():

    try:

        r = requests.get(
            f"{KUCOIN_API}/api/v1/market/allTickers",
            timeout=20
        )

        return r.json().get(
            "data",
            {}
        ).get(
            "ticker",
            []
        )

    except Exception as e:

        print("Ticker error:", e)
        return []


def update_tickers():

    tickers = get_tickers()

    with lock:

        for t in tickers:

            symbol = t.get("symbol")

            if not symbol:
                continue

            try:

                price = float(t["last"])
                low = float(t["low24h"])

            except:
                continue

            latest_price[symbol] = price
            latest_24h_low[symbol] = low


# =========================================================
# MARKET CAP
# =========================================================

def load_market_caps():

    global market_caps

    print("Loading market caps...")

    caps = {}

    for page in range(1, 5):

        try:

            r = requests.get(
                f"{COINGECKO_API}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "sparkline": "false"
                },
                timeout=30
            )

            if r.status_code != 200:
                print(
                    "CoinGecko:",
                    r.status_code
                )
                break

            coins = r.json()

            if not coins:
                break

            for coin in coins:

                symbol = (
                    coin.get("symbol") or ""
                ).upper()

                cap = coin.get("market_cap")

                if symbol and cap:
                    caps[symbol] = max(
                        caps.get(symbol, 0),
                        cap
                    )

            time.sleep(0.5)

        except Exception as e:

            print("Market cap error:", e)
            break

    market_caps = caps

    print(
        "Market caps loaded:",
        len(market_caps)
    )


def is_low_cap(symbol):

    base = symbol.split("-")[0]

    cap = market_caps.get(base)

    if cap is None:
        return False

    return (
        MIN_MARKET_CAP_USD
        <= cap
        <= MAX_MARKET_CAP_USD
    )


# =========================================================
# DEPOSIT STATUS
# =========================================================

def update_deposit_status():

    global deposit_status

    try:

        r = requests.get(
            f"{KUCOIN_API}/api/v3/currencies",
            timeout=30
        )

        data = r.json().get("data", [])

        result = {}

        for coin in data:

            currency = coin.get("currency")

            if not currency:
                continue

            chains = coin.get("chains") or []

            if not chains:
                continue

            any_enabled = False

            for chain in chains:

                if chain.get("isDepositEnabled"):
                    any_enabled = True
                    break

            result[
                currency.upper()
            ] = any_enabled

        deposit_status = result

        print(
            "Deposit status loaded:",
            len(result)
        )

    except Exception as e:

        print(
            "Deposit status error:",
            e
        )


def deposit_off(symbol):

    base = symbol.split("-")[0]

    value = deposit_status.get(base)

    return value is False


# =========================================================
# EARLY PRICE CHECK
# =========================================================

def above_24h_low(symbol):

    price = latest_price.get(symbol)
    low = latest_24h_low.get(symbol)

    if not price or not low:
        return None

    pct = (
        (price - low)
        / low
        * 100
    )

    if pct < -2:
        return None

    if pct > MAX_ABOVE_24H_LOW:
        return None

    return pct


# =========================================================
# CANDLE LOADING
# =========================================================

def parse_candle(c):

    try:

        return {
            "time": int(c[0]),
            "open": float(c[1]),
            "close": float(c[2]),
            "high": float(c[3]),
            "low": float(c[4]),
            "volume": float(c[5])
        }

    except:
        return None


def load_history(symbol):

    if symbol in loaded_history:
        return True

    print(
        "Loading history:",
        symbol
    )

    try:

        for tf, target in [
            ("15min", candles_15m),
            ("1hour", candles_1h)
        ]:

            r = requests.get(
                f"{KUCOIN_API}/api/v1/market/candles",
                params={
                    "symbol": symbol,
                    "type": tf
                },
                timeout=15
            )

            if r.status_code != 200:
                return False

            data = r.json().get(
                "data",
                []
            )

            data = list(
                reversed(data)
            )

            parsed = []

            for c in data[-100:]:

                candle = parse_candle(c)

                if candle:
                    parsed.append(candle)

            target[symbol] = parsed

        loaded_history.add(symbol)

        print(
            "History ready:",
            symbol
        )

        return True

    except Exception as e:

        print(
            "History error:",
            symbol,
            e
        )

        return False


# =========================================================
# LAZY HISTORY LOADING
# =========================================================

def check_candidates():

    for symbol in symbols:

        if not is_low_cap(symbol):
            continue

        pct = above_24h_low(symbol)

        if pct is None:
            continue

        if symbol not in loaded_history:

            load_history(symbol)

        check_signal(symbol)


# =========================================================
# CANDLE STORAGE
# =========================================================

def store_candle(
    symbol,
    timeframe,
    data
):

    candle = parse_candle(data)

    if not candle:
        return

    target = (
        candles_15m
        if timeframe == "15min"
        else candles_1h
    )

    if symbol not in target:
        target[symbol] = []

    arr = target[symbol]

    if (
        arr
        and arr[-1]["time"]
        == candle["time"]
    ):
        arr[-1] = candle

    else:
        arr.append(candle)

    if len(arr) > 100:
        target[symbol] = arr[-100:]


# =========================================================
# 1H PATTERN
# =========================================================

def get_1h_pattern(symbol):

    candles = candles_1h.get(
        symbol,
        []
    )

    if len(candles) < 30:
        return None

    closed = candles[:-1]

    earlier = closed[-30:-12]
    recent = closed[-12:]

    if len(earlier) < 10:
        return None

    base_low = min(
        c["low"]
        for c in earlier
    )

    recent_high = max(
        c["high"]
        for c in recent
    )

    recent_low = min(
        c["low"]
        for c in recent
    )

    recovery = (
        (recent_high - base_low)
        / base_low
        * 100
    )

    if recovery < MIN_RECOVERY_PCT:
        return None

    # Correct retracement calculation
    retrace = (
        (recent_high - recent_low)
        / recent_high
        * 100
    )

    if retrace > MAX_RETRACE_PCT:
        return None

    mid = len(recent) // 2

    low1 = min(
        c["low"]
        for c in recent[:mid]
    )

    low2 = min(
        c["low"]
        for c in recent[mid:]
    )

    if low2 < low1 * 0.995:
        return None

    return {
        "recovery": recovery,
        "retrace": retrace,
        "recent_low": recent_low,
        "recent_high": recent_high
    }


# =========================================================
# 15M PATTERN
# =========================================================

def get_15m_pattern(symbol):

    candles = candles_15m.get(
        symbol,
        []
    )

    if len(candles) < 25:
        return None

    closed = candles[:-1]

    recent = closed[-10:]
    previous = closed[-20:-10]

    high = max(
        c["high"]
        for c in recent
    )

    low = min(
        c["low"]
        for c in recent
    )

    if low <= 0:
        return None

    range_pct = (
        (high - low)
        / low
        * 100
    )

    if range_pct > 12:
        return None

    support = min(
        c["low"]
        for c in recent[:-3]
    )

    for c in recent[-3:]:

        if c["close"] < support * 0.985:
            return None

    avg_volume = sum(
        c["volume"]
        for c in previous
    ) / len(previous)

    recent_volume = sum(
        c["volume"]
        for c in recent[-3:]
    ) / 3

    if avg_volume <= 0:
        return None

    volume_multiplier = (
        recent_volume
        / avg_volume
    )

    if volume_multiplier < MIN_VOLUME_MULTIPLIER:
        return None

    price = latest_price.get(symbol)

    if not price:
        return None

    # Near resistance
    resistance_distance = (
        (high - price)
        / price
        * 100
    )

    if resistance_distance < 0:
        resistance_distance = 0

    if resistance_distance > 8:
        return None

    return {
        "support": support,
        "resistance": high,
        "range": range_pct,
        "volume": volume_multiplier
    }


# =========================================================
# TIME FILTER
# =========================================================

def time_state():

    hour = datetime.now(
        BD_TZ
    ).hour

    # No alerts
    if 1 <= hour < 7:
        return "OFF"

    # Priority
    if 9 <= hour < 11:
        return "PRIORITY"

    if 19 <= hour < 23:
        return "PRIORITY"

    return "NORMAL"


# =========================================================
# SIGNAL
# =========================================================

def check_signal(symbol):

    if symbol not in loaded_history:
        return

    state = time_state()

    if state == "OFF":
        return

    pct = above_24h_low(symbol)

    if pct is None:
        return

    h1 = get_1h_pattern(symbol)

    if not h1:
        return

    m15 = get_15m_pattern(symbol)

    if not m15:
        return

    now = time.time()

    previous = last_signal.get(
        symbol,
        0
    )

    if (
        now - previous
        < SIGNAL_COOLDOWN
    ):
        return

    price = latest_price.get(
        symbol
    )

    low = latest_24h_low.get(
        symbol
    )

    if not price or not low:
        return

    dep_off = deposit_off(
        symbol
    )

    # Score
    score = 80

    if dep_off:
        score += 15

    if state == "PRIORITY":
        score += 5

    if pct <= 10:
        score += 5

    if score < 80:
        return

    last_signal[symbol] = now

    cap = market_caps.get(
        symbol.split("-")[0],
        0
    )

    message = f"""
🚨 EARLY BUY SETUP

{symbol}

💰 Price: {price:.10g}
📉 24H Low: {low:.10g}
📊 Above Low: +{pct:.1f}%

📈 1H
Recovery: +{h1['recovery']:.1f}%
Retrace: {h1['retrace']:.1f}%
Higher Low: ✅

⏱ 15M
Support: {m15['support']:.10g}
Resistance: {m15['resistance']:.10g}
Volume: {m15['volume']:.1f}x

🔴 Deposit OFF:
{"YES" if dep_off else "NO"}

💎 Market Cap:
${cap:,.0f}

⏰ {state} WINDOW

🔥 EARLY PATTERN MATCH
"""

    print(message)

    send_telegram(
        message.strip()
    )


# =========================================================
# FAST TICKER LOOP
# =========================================================

def ticker_loop():

    while True:

        try:

            update_tickers()

            # Only load history for
            # coins currently near 24H low
            check_candidates()

        except Exception as e:

            print(
                "Ticker loop error:",
                e
            )

        time.sleep(20)


# =========================================================
# DEPOSIT LOOP
# =========================================================

def deposit_loop():

    while True:

        update_deposit_status()

        time.sleep(300)


# =========================================================
# WEBSOCKET
# =========================================================

def websocket_worker():

    while True:

        try:

            r = requests.post(
                f"{KUCOIN_API}/api/v1/bullet-public",
                timeout=20
            )

            data = r.json()["data"]

            token = data["token"]

            server = (
                data["instanceServers"][0]
            )

            endpoint = server["endpoint"]

            ws = websocket.create_connection(
                f"{endpoint}?token={token}",
                timeout=30
            )

            print(
                "✅ WebSocket connected"
            )

            sub_id = 1

            for symbol in symbols:

                for tf in [
                    "15min",
                    "1hour"
                ]:

                    ws.send(
                        json.dumps({
                            "id": str(sub_id),
                            "type": "subscribe",
                            "topic":
                                f"/market/candles:"
                                f"{symbol}_{tf}",
                            "privateChannel":
                                False,
                            "response":
                                True
                        })
                    )

                    sub_id += 1

                time.sleep(0.02)

            print(
                "✅ WebSocket subscriptions ready"
            )

            while True:

                try:

                    raw = ws.recv()

                    if not raw:
                        continue

                    msg = json.loads(raw)

                    if msg.get(
                        "type"
                    ) != "message":
                        continue

                    topic = msg.get(
                        "topic",
                        ""
                    )

                    data = msg.get(
                        "data"
                    )

                    if not data:
                        continue

                    if "/market/candles:" not in topic:
                        continue

                    pair = topic.split(
                        "/market/candles:"
                    )[1]

                    if pair.endswith(
                        "_15min"
                    ):

                        symbol = pair.replace(
                            "_15min",
                            ""
                        )

                        store_candle(
                            symbol,
                            "15min",
                            data["candles"]
                        )

                        check_signal(
                            symbol
                        )

                    elif pair.endswith(
                        "_1hour"
                    ):

                        symbol = pair.replace(
                            "_1hour",
                            ""
                        )

                        store_candle(
                            symbol,
                            "1hour",
                            data["candles"]
                        )

                        check_signal(
                            symbol
                        )

                except websocket.WebSocketTimeoutException:

                    continue

        except Exception as e:

            print(
                "WebSocket error:",
                e
            )

            time.sleep(5)


# =========================================================
# MAIN
# =========================================================

def main():

    global symbols

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print(
            "ERROR: TELEGRAM_TOKEN / CHAT_ID missing"
        )

        return

    print(
        "================================="
    )

    print(
        "KUCOIN EARLY BUY DETECTOR"
    )

    print(
        "================================="
    )

    # 1. Symbols
    symbols = get_symbols()

    if not symbols:
        return

    # 2. Market cap
    load_market_caps()

    # 3. Low-cap only
    symbols = [
        s for s in symbols
        if is_low_cap(s)
    ]

    print(
        "Low-cap pairs:",
        len(symbols)
    )

    # 4. Initial ticker
    update_tickers()

    # 5. Deposit status
    update_deposit_status()

    print(
        "⚡ Starting real-time monitoring..."
    )

    # IMPORTANT:
    # Do NOT load history for every coin.
    # ticker_loop will load it only when
    # price enters <=15% above 24H low.

    threading.Thread(
        target=ticker_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=deposit_loop,
        daemon=True
    ).start()

    # WebSocket
    websocket_worker()


if __name__ == "__main__":
    main()
