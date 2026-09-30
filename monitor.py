import os
import json
import time
import requests
from datetime import datetime, timezone

# ============================================================
# KuCoin Daily 2nd-Move / Retest Scanner
# ============================================================

KUCOIN_API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ---------------- SETTINGS ----------------

TIMEFRAME = "1day"

# Strong initial move
MIN_STRONG_MOVE = 8.0          # minimum % move of breakout candle
MIN_BREAKOUT_ABOVE = 3.0       # breakout must beat previous resistance by %

# Retest
RETEST_DISTANCE = 3.5          # % distance from breakout zone
MIN_PULLBACK = 4.0             # minimum pullback from post-breakout high
MAX_PULLBACK = 35.0            # don't alert after a huge collapse

# Previous resistance
LOOKBACK = 20

# Avoid very new / illiquid pairs
MIN_PRICE = 0.000001
MIN_VOLUME_USDT = 50000

# State file
STATE_FILE = "alert_state.json"

# ============================================================
# HTTP
# ============================================================

session = requests.Session()
session.headers.update({
    "User-Agent": "KuCoin-Daily-Retest-Scanner/1.0"
})


def kucoin_get(path, params=None):
    try:
        r = session.get(
            KUCOIN_API + path,
            params=params,
            timeout=20
        )
        r.raise_for_status()

        data = r.json()

        if data.get("code") != "200000":
            return None

        return data.get("data")

    except Exception:
        return None


# ============================================================
# TELEGRAM
# ============================================================

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
        r = requests.post(
            url,
            json=payload,
            timeout=20
        )

        if r.ok:
            print("Telegram alert sent.")
            return True

        print("Telegram error:", r.text)
        return False

    except Exception as e:
        print("Telegram exception:", e)
        return False


# ============================================================
# STATE
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print("State save error:", e)


# ============================================================
# SYMBOLS
# ============================================================

def get_symbols():
    data = kucoin_get("/api/v2/symbols")

    if not data:
        return []

    symbols = []

    for item in data:
        try:
            if item.get("quoteCurrency") != "USDT":
                continue

            if not item.get("enableTrading"):
                continue

            symbol = item.get("symbol")

            if symbol:
                symbols.append(symbol)

        except Exception:
            continue

    return symbols


# ============================================================
# DAILY CANDLES
# ============================================================

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

    for c in data:

        try:
            # KuCoin:
            # [time, open, close, high, low, volume, turnover]

            candles.append({
                "time": int(c[0]),
                "open": float(c[1]),
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4]),
                "volume": float(c[5]),
                "turnover": float(c[6])
            })

        except Exception:
            continue

    candles.sort(key=lambda x: x["time"])

    return candles


# ============================================================
# CURRENT PRICE
# ============================================================

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


# ============================================================
# DETECT SETUP
# ============================================================

def detect_retest(symbol, candles, current_price):

    if len(candles) < LOOKBACK + 10:
       
