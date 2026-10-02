import os
import time
import json
import threading
from datetime import datetime, timezone
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

# Low-cap definition
MAX_MARKET_CAP_USD = float(
    os.getenv("MAX_MARKET_CAP_USD", "300000000")
)

MIN_MARKET_CAP_USD = float(
    os.getenv("MIN_MARKET_CAP_USD", "1000000")
)

# Early entry
MAX_ABOVE_24H_LOW = 15.0

# TA
MIN_RECOVERY_PCT = 15.0
MAX_RETRACE_PCT = 50.0
MIN_VOLUME_MULTIPLIER = 1.5

# Price should not already be too far from recent range
MAX_1H_EXTENSION_PCT = 12.0

# Signal cooldown
SIGNAL_COOLDOWN_SECONDS = 12 * 60 * 60

# Timezone
BD_TZ = ZoneInfo("Asia/Dhaka")

# Alert windows
NO_ALERT_START = 1
NO_ALERT_END = 7

PRIORITY_MORNING_START = 9
PRIORITY_MORNING_END = 11

PRIORITY_NIGHT_START = 19
PRIORITY_NIGHT_END = 23


# =========================================================
# GLOBAL DATA
# =========================================================

symbols = []
market_caps = {}

candles_15m = {}
candles_1h = {}

latest_price = {}
latest_24h_low = {}
deposit_status = {}

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

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        print("Telegram:", r.status_code)
    except Exception as e:
        print("Telegram error:", e)


# =========================================================
# KUCOIN API
# =========================================================

def get_symbols():
    url = f"{KUCOIN_API}/api/v2/symbols"

    try:
        data = requests.get(url, timeout=15).json()

        result = []

        for x in data.get("data", []):
            if x.get("quoteCurrency") != "USDT":
                continue

            if not x.get("enableTrading"):
                continue

            symbol = x.get("symbol")

            if symbol:
                result.append(symbol)

        print("USDT symbols:", len(result))
        return result

    except Exception as e:
        print("Symbol error:", e)
        return []


def get_all_tickers():
    url = f"{KUCOIN_API}/api/v1/market/allTickers"

    try:
        data = requests.get(url, timeout=15).json()
        return data.get("data", {}).get("ticker", [])

    except Exception as e:
        print("Ticker error:", e)
        return []


# =========================================================
# COINGECKO MARKET CAP
# =========================================================

def load_market_caps():
    global market_caps

    print("Loading market caps...")

    new_caps = {}

    # First 4 pages = up to ~1000 coins
    for page in range(1, 5):

        try:
            url = f"{COINGECKO_API}/coins/markets"

            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false"
            }

            r = requests.get(
                url,
                params=params,
                timeout=30
            )

            if r.status_code != 200:
                print(
                    "CoinGecko status:",
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
                    # Store maximum cap seen for duplicate symbols
                    if symbol not in new_caps:
                        new_caps[symbol] = cap
                    else:
                        new_caps[symbol] = max(
                            new_caps[symbol],
                            cap
                        )

            time.sleep(1)

        except Exception as e:
            print(
                "CoinGecko error:",
                e
            )
            break

    market_caps = new_caps

    print(
        "Market-cap symbols loaded:",
        len(market_caps)
    )


# =========================================================
# LOW-CAP FILTER
# =========================================================

def is_low_cap(symbol):

    base = symbol.split("-")[0]

    cap = market_caps.get(base)

    if cap is None:
        return False

    if cap < MIN_MARKET_CAP_USD:
        return False

    if cap > MAX_MARKET_CAP_USD:
        return False

    return True


# =========================================================
# DEPOSIT STATUS
# =========================================================

def update_deposit_status():
    """
    KuCoin currency endpoint is checked periodically.
    """

    global deposit_status

    url = f"{KUCOIN_API}/api/v3/currencies"

    try:
        r = requests.get(url, timeout=20)

        if r.status_code != 200:
            print(
                "Currency status:",
                r.status_code
            )
            return

        data = r.json().get("data", [])

        new_status = {}

        for coin in data:

            currency = coin.get("currency")

            if not currency:
                continue

            chains = coin.get("chains") or []

            if not chains:
                continue

            enabled = False

            for chain in chains:

                if chain.get("isDepositEnabled"):
                    enabled = True
                    break

            new_status[currency.upper()] = enabled

        deposit_status = new_status

        print(
            "Deposit status updated:",
            len(deposit_status)
        )

    except Exception as e:
        print(
            "Deposit status error:",
            e
        )


def is_deposit_off(symbol):

    base = symbol.split("-")[0]

    status = deposit_status.get(base)

    if status is None:
        return False

    return status is False


# =========================================================
# TICKER UPDATE
# =========================================================

def ticker_loop():

    while True:

        try:
            tickers = get_all_tickers()

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

        except Exception as e:
            print("Ticker loop:", e)

        time.sleep(20)


# =========================================================
# CANDLE HELPERS
# =========================================================

def candle_to_dict(data):

    # KuCoin websocket candle format:
    # [timestamp, open, close, high, low, volume, turnover]

    try:

        return {
            "time": int(data[0]),
            "open": float(data[1]),
            "close": float(data[2]),
            "high": float(data[3]),
            "low": float(data[4]),
            "volume": float(data[5]),
            "turnover": float(data[6])
        }

    except:
        return None


def store_candle(symbol, timeframe, data):

    candle = candle_to_dict(data)

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

    if arr and arr[-1]["time"] == candle["time"]:
        arr[-1] = candle
    else:
        arr.append(candle)

    # Keep enough history
    if len(arr) > 100:
        target[symbol] = arr[-100:]


# =========================================================
# TECHNICAL ANALYSIS
# =========================================================

def percentage(a, b):

    if b == 0:
        return 0

    return ((a - b) / b) * 100


def get_1h_pattern(symbol):

    candles = candles_1h.get(symbol, [])

    if len(candles) < 30:
        return None

    closed = candles[:-1]

    if len(closed) < 25:
        return None

    recent = closed[-12:]

    # Recent swing low
    recent_low = min(
        c["low"]
        for c in recent
    )

    recent_high = max(
        c["high"]
        for c in recent
    )

    # Find earlier base
    earlier = closed[-30:-12]

    if not earlier:
        return None

    base_low = min(
        c["low"]
        for c in earlier
    )

    recovery = percentage(
        recent_high,
        base_low
    )

    if recovery < MIN_RECOVERY_PCT:
        return None

    # Retracement from recent high
    retrace = percentage(
        recent_high,
        recent_low
    )

    retrace = abs(retrace)

    if retrace > MAX_RETRACE_PCT:
        return None

    # Higher-low check
    mid = len(recent) // 2

    first_half = recent[:mid]
    second_half = recent[mid:]

    low1 = min(
        c["low"]
        for c in first_half
    )

    low2 = min(
        c["low"]
        for c in second_half
    )

    higher_low = low2 > low1 * 0.995

    if not higher_low:
        return None

    # Current price
    price = latest_price.get(symbol)

    if not price:
        return None

    extension = percentage(
        price,
        recent_low
    )

    if extension > MAX_1H_EXTENSION_PCT:
        return None

    return {
        "base_low": base_low,
        "recent_low": recent_low,
        "recent_high": recent_high,
        "recovery": recovery,
        "retrace": retrace,
        "higher_low": higher_low
    }


def get_15m_confirmation(symbol):

    candles = candles_15m.get(symbol, [])

    if len(candles) < 25:
        return None

    closed = candles[:-1]

    recent = closed[-10:]

    # Range
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
        (high - low) / low
    ) * 100

    # We want consolidation, not huge expansion
    if range_pct > 12:
        return None

    # Support hold
    support = min(
        c["low"]
        for c in recent[:-3]
    )

    last_candles = recent[-3:]

    support_holds = all(
        c["close"] >= support * 0.985
        for c in last_candles
    )

    if not support_holds:
        return None

    # Volume expansion
    old = closed[-20:-10]

    if not old:
        return None

    avg_volume = sum(
        c["volume"]
        for c in old
    ) / len(old)

    recent_volume = sum(
        c["volume"]
        for c in recent[-3:]
    ) / 3

    if avg_volume <= 0:
        return None

    volume_multiplier = (
        recent_volume / avg_volume
    )

    if volume_multiplier < MIN_VOLUME_MULTIPLIER:
        return None

    # Resistance
    resistance = high

    price = latest_price.get(symbol)

    if not price:
        return None

    resistance_distance = percentage(
        resistance,
        price
    )

    # Price should be near resistance,
    # not far below it
    if resistance_distance < 0:
        resistance_distance = 0

    if resistance_distance > 8:
        return None

    return {
        "support": support,
        "resistance": resistance,
        "range_pct": range_pct,
        "volume_multiplier": volume_multiplier,
        "resistance_distance": resistance_distance
    }


# =========================================================
# EARLY ENTRY FILTER
# =========================================================

def early_price_filter(symbol):

    price = latest_price.get(symbol)
    low = latest_24h_low.get(symbol)

    if not price or not low:
        return None

    above_low = percentage(
        price,
        low
    )

    # HARD FILTER
    if above_low > MAX_ABOVE_24H_LOW:
        return None

    # Avoid weird ticker data
    if above_low < -2:
        return None

    return above_low


# =========================================================
# TIME FILTER
# =========================================================

def get_time_state():

    now = datetime.now(BD_TZ)

    hour = now.hour

    # No signal 01:00–07:00
    if (
        hour >= NO_ALERT_START
        and hour < NO_ALERT_END
    ):
        return "OFF"

    # Priority windows
    if (
        PRIORITY_MORNING_START
        <= hour
        < PRIORITY_MORNING_END
    ):
        return "PRIORITY"

    if (
        PRIORITY_NIGHT_START
        <= hour
        < PRIORITY_NIGHT_END
    ):
        return "PRIORITY"

    return "NORMAL"


# =========================================================
# SIGNAL ENGINE
# =========================================================

def check_signal(symbol):

    if not is_low_cap(symbol):
        return

    time_state = get_time_state()

    if time_state == "OFF":
        return

    above_low = early_price_filter(symbol)

    if above_low is None:
        return

    h1 = get_1h_pattern(symbol)

    if not h1:
        return

    m15 = get_15m_confirmation(symbol)

    if not m15:
        return

    price = latest_price.get(symbol)

    if not price:
        return

    deposit_off = is_deposit_off(symbol)

    # Stronger when deposit is OFF
    score = 0

    score += 30
    score += 25
    score += 20

    if deposit_off:
        score += 15

    if time_state == "PRIORITY":
        score += 10

    if above_low <= 10:
        score += 5

    # Only alert strong setups
    if score < 80:
        return

    now = time.time()

    previous = last_signal.get(symbol, 0)

    if now - previous < SIGNAL_COOLDOWN_SECONDS:
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
📉 24H Low: {latest_24h_low.get(symbol, 0):.10g}
📊 Above 24H Low: +{above_low:.1f}%

📈 1H Structure
Recovery: +{h1['recovery']:.1f}%
Retrace: {h1['retrace']:.1f}%
Higher Low: ✅

⏱ 15M Structure
Consolidation: {m15['range_pct']:.1f}%
Support: {m15['support']:.10g}
Resistance: {m15['resistance']:.10g}
Volume: {m15['volume_multiplier']:.1f}x

🔴 Deposit OFF: {"YES" if deposit_off else "NO"}

💎 Market Cap: ${cap:,.0f}
⏰ Window: {time_state}

🔥 Pattern Match: STRONG
🎯 Early Entry: YES
"""

    print(message)

    send_telegram(
        message.strip()
    )


# =========================================================
# WEBSOCKET
# =========================================================

def get_ws_token():

    url = (
        f"{KUCOIN_API}"
        "/api/v1/bullet-public"
    )

    r = requests.post(
        url,
        timeout=15
    )

    data = r.json()["data"]

    token = data["token"]

    server = data["instanceServers"][0]

    endpoint = server["endpoint"]

    ping_interval = (
        server.get("pingInterval", 18000)
        / 1000
    )

    return token, endpoint, ping_interval


def websocket_worker():

    while True:

        try:

            token, endpoint, ping_interval = (
                get_ws_token()
            )

            connect_url = (
                f"{endpoint}"
                f"?token={token}"
            )

            ws = websocket.create_connection(
                connect_url,
                timeout=30
            )

            print("WebSocket connected")

            sub_id = 1

            # Subscribe in chunks
            for i in range(
                0,
                len(symbols),
                50
            ):

                chunk = symbols[i:i + 50]

                # 15m candles
                for symbol in chunk:

                    ws.send(
                        json.dumps({
                            "id": str(sub_id),
                            "type": "subscribe",
                            "topic": (
                                f"/market/candles:"
                                f"{symbol}_15min"
                            ),
                            "privateChannel": False,
                            "response": True
                        })
                    )

                    sub_id += 1

                    # 1h candles
                    ws.send(
                        json.dumps({
                            "id": str(sub_id),
                            "type": "subscribe",
                            "topic": (
                                f"/market/candles:"
                                f"{symbol}_1hour"
                            ),
                            "privateChannel": False,
                            "response": True
                        })
                    )

                    sub_id += 1

                time.sleep(0.5)

            last_ping = time.time()

            while True:

                if (
                    time.time() - last_ping
                    > ping_interval / 2
                ):
                    ws.send(
                        json.dumps({
                            "id": str(
                                int(time.time())
                            ),
                            "type": "ping"
                        })
                    )

                    last_ping = time.time()

                ws.settimeout(5)

                try:
                    raw = ws.recv()

                except websocket.WebSocketTimeoutException:
                    continue

                if not raw:
                    continue

                try:
                    msg = json.loads(raw)
                except:
                    continue

                if msg.get("type") != "message":
                    continue

                topic = msg.get("topic", "")

                data = msg.get("data")

                if not data:
                    continue

                # Topic:
                # /market/candles:BTC-USDT_15min

                if "/market/candles:" not in topic:
                    continue

                try:

                    pair = topic.split(
                        "/market/candles:"
                    )[1]

                    if pair.endswith("_15min"):

                        symbol = pair.replace(
                            "_15min",
                            ""
                        )

                        with lock:
                            store_candle(
                                symbol,
                                "15min",
                                data["candles"]
                            )

                        check_signal(symbol)

                    elif pair.endswith("_1hour"):

                        symbol = pair.replace(
                            "_1hour",
                            ""
                        )

                        with lock:
                            store_candle(
                                symbol,
                                "1hour",
                                data["candles"]
                            )

                        check_signal(symbol)

                except Exception as e:
                    print(
                        "WS message error:",
                        e
                    )

        except Exception as e:

            print(
                "WebSocket disconnected:",
                e
            )

            time.sleep(5)


# =========================================================
# INITIAL CANDLE LOAD
# =========================================================

def load_initial_candles():

    print("Loading initial candles...")

    for index, symbol in enumerate(symbols):

        try:

            for timeframe, tf in [
                ("15min", "15min"),
                ("1hour", "1hour")
            ]:

                url = (
                    f"{KUCOIN_API}"
                    "/api/v1/market/candles"
                )

                params = {
                    "symbol": symbol,
                    "type": tf
                }

                r = requests.get(
                    url,
                    params=params,
                    timeout=10
                )

                if r.status_code != 200:
                    continue

                data = r.json().get(
                    "data",
                    []
                )

                # KuCoin returns newest first
                data = list(reversed(data))

                target = (
                    candles_15m
                    if timeframe == "15min"
                    else candles_1h
                )

                target[symbol] = []

                for candle in data[-100:]:

                    c = candle_to_dict(candle)

                    if c:
                        target[symbol].append(c)

            if index % 20 == 0:
                print(
                    "Initial candles:",
                    index,
                    "/",
                    len(symbols)
                )

            # Avoid hammering API
            time.sleep(0.12)

        except Exception as e:

            print(
                "Initial candle error:",
                symbol,
                e
            )


# =========================================================
# DEPOSIT REFRESH LOOP
# =========================================================

def deposit_loop():

    while True:

        update_deposit_status()

        # Every 5 minutes
        time.sleep(300)


# =========================================================
# MARKET CAP REFRESH LOOP
# =========================================================

def market_cap_loop():

    while True:

        load_market_caps()

        # Refresh every 2 hours
        time.sleep(7200)


# =========================================================
# MAIN
# =========================================================

def main():

    global symbols

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print(
            "ERROR: TELEGRAM_TOKEN / CHAT_ID "
            "environment variables missing."
        )

        return

    print(
        "======================================"
    )

    print(
        "KuCoin Influencer-Style Early Buyer"
    )

    print(
        "======================================"
    )

    symbols = get_symbols()

    if not symbols:
        print("No symbols found.")
        return

    # Load market cap first
    load_market_caps()

    # Keep only low-cap established coins
    symbols = [
        s for s in symbols
        if is_low_cap(s)
    ]

    print(
        "Low-cap symbols:",
        len(symbols)
    )

    if not symbols:
        print(
            "No low-cap symbols matched."
        )
        return

    update_deposit_status()

    # Initial historical candles
    load_initial_candles()

    # Background ticker
    threading.Thread(
        target=ticker_loop,
        daemon=True
    ).start()

    # Deposit status
    threading.Thread(
        target=deposit_loop,
        daemon=True
    ).start()

    # Market cap refresh
    threading.Thread(
        target=market_cap_loop,
        daemon=True
    ).start()

    # WebSocket
    websocket_worker()


if __name__ == "__main__":
    main()
