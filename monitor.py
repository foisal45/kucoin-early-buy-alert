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

# Early-buy footprint
MIN_BUY_USD = 250
WINDOW_SECONDS = 120
MIN_BUY_COUNT = 3
BUY_SELL_RATIO = 1.40

# Price zone
MAX_ABOVE_LOW = 20.0
MIN_FIRST_MOVE = 15.0
MIN_RETRACE = 8.0

# Listing filter
MIN_LISTED_DAYS = 30

# Same coin alert cooldown
ALERT_COOLDOWN = 6 * 60 * 60

# Trade memory
MAX_TRADES = 500


# =========================================================
# GLOBAL DATA
# =========================================================

trade_data = defaultdict(
    lambda: deque(maxlen=MAX_TRADES)
)

last_alert = {}

lock = threading.Lock()


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
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

        if r.status_code != 200:
            print(r.text)
            return False

        return True

    except Exception as e:
        print("Telegram error:", e)
        return False


# =========================================================
# SYMBOLS
# =========================================================

def get_symbols():

    url = f"{KUCOIN_API}/api/v2/symbols"

    try:

        r = requests.get(
            url,
            timeout=20
        )

        r.raise_for_status()

        data = r.json().get(
            "data",
            []
        )

    except Exception as e:

        print("Symbol API error:", e)
        return []

    now_ms = int(
        time.time() * 1000
    )

    symbols = []

    for item in data:

        try:

            symbol = item.get("symbol")

            if item.get(
                "quoteCurrency"
            ) != "USDT":
                continue

            if not item.get(
                "enableTrading"
            ):
                continue

            list_at = item.get("listAt")

            if list_at:

                age_days = (
                    now_ms - int(list_at)
                ) / 1000 / 86400

                if age_days < MIN_LISTED_DAYS:
                    continue

            symbols.append(symbol)

        except Exception:
            continue

    print(
        f"USDT symbols loaded: {len(symbols)}"
    )

    return symbols


# =========================================================
# 24H DATA
# =========================================================

def get_24h_data():

    url = (
        f"{KUCOIN_API}/api/v1/"
        f"market/allTickers"
    )

    try:

        r = requests.get(
            url,
            timeout=20
        )

        r.raise_for_status()

        tickers = r.json().get(
            "data", {}
        ).get(
            "ticker", []
        )

    except Exception as e:

        print("Ticker error:", e)
        return {}

    result = {}

    for t in tickers:

        symbol = t.get("symbol")

        if not symbol:
            continue

        if not symbol.endswith("-USDT"):
            continue

        try:

            result[symbol] = {
                "price": float(t["last"]),
                "low": float(t["low"]),
                "high": float(t["high"]),
                "change": float(
                    t["changeRate"]
                ) * 100,
                "volume": float(
                    t["volValue"]
                )
            }

        except Exception:
            continue

    return result


# =========================================================
# 15M CANDLES
# =========================================================

def get_candles(symbol):

    end = int(time.time())

    start = end - (
        24 * 60 * 60
    )

    url = (
        f"{KUCOIN_API}/api/v1/"
        f"market/candles"
    )

    params = {
        "symbol": symbol,
        "type": "15min",
        "startAt": start,
        "endAt": end
    }

    try:

        r = requests.get(
            url,
            params=params,
            timeout=15
        )

        if r.status_code != 200:
            return []

        raw = r.json().get(
            "data",
            []
        )

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

    except Exception as e:

        print(
            f"Candle error {symbol}: {
