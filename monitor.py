import os
import json
import time
import requests
from datetime import datetime, timezone

KUCOIN_API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

TIMEFRAME = "1day"

MIN_STRONG_MOVE = 8.0
MIN_BREAKOUT_ABOVE = 3.0

RETEST_DISTANCE = 3.5
MIN_PULLBACK = 4.0
MAX_PULLBACK = 35.0

LOOKBACK = 20
MIN_PRICE = 0.000001
MIN_VOLUME_USDT = 50000

STATE_FILE = "alert_state.json"

session = requests.Session()
session.headers.update({
    "User-Agent": "KuCoin-Daily-Retest-Scanner/1.0"
})


def kucoin_get(path, params=None):
    try:
        response = session.get(
            KUCOIN_API + path,
            params=params,
            timeout=20
        )

        response.raise_for_status()
        data = response.json()

        if data.get("code") != "200000":
            return None

        return data.get("data")

    except Exception as error:
        print(f"API error: {error}")
        return None


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return False

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
        response = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if response.ok:
            print("Telegram alert sent.")
            return True

        print("Telegram error:", response.text)
        return False

    except Exception as error:
        print(f"Telegram exception: {error}")
        return False


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r") as file:
            return json.load(file)

    except Exception as error:
        print(f"State load error: {error}")
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as file:
            json.dump(state, file, indent=2)

    except Exception as error:
        print(f"State save error: {error}")


def get_symbols():
    data = kucoin_get("/api/v2/symbols")

    if not data:
        return []

    symbols = []

    for item in data:
        if item.get("quoteCurrency") != "USDT":
            continue

        if not item.get("enableTrading"):
            continue

        symbol = item.get("symbol")

        if symbol:
            symbols.append(symbol)

    return symbols


def get_daily_candles(symbol):
    data = kucoin_get(
        "/api/v1/market/candles",
        {
            "symbol": symbol,
            "type": TIMEFRAME
        }
    )

    if not data:
        return []

    candles = []

    for candle in data:
        try:
            candles.append({
                "time": int(candle[0]),
                "open": float(candle[1]),
                "close": float(candle[2]),
                "high": float(candle[3]),
                "low": float(candle[4]),
                "volume": float(candle[5]),
                "turnover": float(candle[6])
            })

        except Exception:
            continue

    candles.sort(key=lambda x: x["time"])

    return candles


def get_current_price(symbol):
    data = kucoin_get(
        "/api/v1/market/orderbook/level1",
        {
            "symbol": symbol
        }
    )

    if not data:
        return None

    try:
        return float(data["price"])

    except Exception:
        return None


def detect_retest(symbol, candles, current_price):

    if len(candles) < LOOKBACK + 10:
        return None

    # Ignore currently forming daily candle
    completed = candles[:-1]

    if len(completed) < LOOKBACK + 8:
        return None

    candidates = []

    start = max(
        LOOKBACK,
        len(completed) - 25
    )

    for index in range(
        start,
        len(completed) - 3
    ):

        candle = completed[index]

        previous = completed[
            index - LOOKBACK:index
        ]

        previous_high = max(
            item["high"] for item in previous
        )

        if previous_high <= 0:
            continue

        breakout_strength = (
            (candle["close"] - previous_high)
            / previous_high
        ) * 100

        candle_move = (
            (candle["close"] - candle["open"])
            / candle["open"]
        ) * 100

        if breakout_strength < MIN_BREAKOUT_ABOVE:
            continue

        if candle_move < MIN_STRONG_MOVE:
            continue

        if candle["turnover"] < MIN_VOLUME_USDT:
            continue

        candidates.append({
            "index": index,
            "breakout": candle,
            "zone": previous_high
        })

    if not candidates:
        return None

    setup = candidates[-1]

    breakout_index = setup["index"]
    breakout_candle = setup["breakout"]
    breakout_zone = setup["zone"]

    after_breakout = completed[
        breakout_index + 1:
    ]

    if len(after_breakout) < 2:
        return None

    post_breakout_high = max(
        item["
