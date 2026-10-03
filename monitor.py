import os
import time
import requests


# =========================================================
# CONFIG
# =========================================================

KUCOIN_API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ---- Setup rules ----

# Coin must first make at least this much move from 24H low
MIN_FIRST_MOVE = 20.0

# Price must come back within +15% of 24H low
MAX_ABOVE_LOW = 15.0

# Price must retrace at least 15% from the pump high
MIN_RETRACE = 15.0

# Exclude newly listed coins
MIN_LISTED_DAYS = 30

# Buy activity:
# Current 15M candle should be green and volume should be
# higher than the average of previous candles.
VOLUME_MULTIPLIER = 1.20


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN or CHAT_ID missing.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:
        r = requests.post(
            url,
            json=payload,
            timeout=15
        )

        print("Telegram:", r.status_code)

        if r.status_code != 200:
            print(r.text)

    except Exception as e:
        print("Telegram error:", e)


# =========================================================
# GET EXISTING KUCOIN COINS
# =========================================================

def get_symbols():
    url = f"{KUCOIN_API}/api/v2/symbols"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()

        data = r.json().get("data", [])

    except Exception as e:
        print("Symbol API error:", e)
        return []

    now_ms = int(time.time() * 1000)

    symbols = []

    for item in data:

        try:
            symbol = item.get("symbol")

            # Only USDT spot
            if item.get("quoteCurrency") != "USDT":
                continue

            # Must be tradable
            if not item.get("enableTrading"):
                continue

            # -------------------------------------------------
            # Exclude newly listed coins
            # -------------------------------------------------

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

    print(f"Existing USDT coins: {len(symbols)}")

    return symbols


# =========================================================
# 15 MIN CANDLES
# =========================================================

def get_candles(symbol):

    end = int(time.time())
    start = end - (24 * 60 * 60)

    url = f"{KUCOIN_API}/api/v1/market/candles"

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

        raw = r.json().get("data", [])

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
            f"Candle error {symbol}: {e}"
        )

        return []


# =========================================================
# CHECK RETEST SETUP
# =========================================================

def check_setup(symbol):

    candles = get_candles(symbol)

    if len(candles) < 20:
        return None

    # -------------------------------------------------
    # Use completed 15M candles
    # -------------------------------------------------

    # Current/latest candle can still be forming.
    # Use the previous completed candle.
    current = candles[-2]

    previous = candles[:-2]

    if len(previous) < 10:
        return None

    # -------------------------------------------------
    # 24H LOW
    # -------------------------------------------------

    low_24h = min(
        c["low"]
        for c in candles
    )

    if low_24h <= 0:
        return None

    # -------------------------------------------------
    # FIRST MOVE / PUMP
    # -------------------------------------------------

    peak_candle = max(
        previous,
        key=lambda c: c["high"]
    )

    peak = peak_candle["high"]

    first_move = (
        (peak - low_24h)
        / low_24h
    ) * 100

    # Must have a meaningful first move
    if first_move < MIN_FIRST_MOVE:
        return None

    # -------------------------------------------------
    # IMPORTANT:
    # Peak must happen BEFORE current candle
    # -------------------------------------------------

    peak_index = previous.index(
        peak_candle
    )

    if peak_index >= len(previous) - 1:
        return None

    # -------------------------------------------------
    # CURRENT PRICE
    # -------------------------------------------------

    current_price = current["close"]

    above_low = (
        (current_price - low_24h)
        / low_24h
    ) * 100

    # Current price must be inside
    # 24H Low +15% zone
    if above_low < 0:
        return None

    if above_low > MAX_ABOVE_LOW:
        return None

    # -------------------------------------------------
    # RETRACE
    # -------------------------------------------------

    retrace = (
        (peak - current_price)
        / peak
    ) * 100

    if retrace < MIN_RETRACE:
        return None

    # -------------------------------------------------
    # BUYING ACTIVITY
    #
    # We don't use WebSocket.
    # Instead:
    #
    # 1. Current completed 15M candle is green
    # 2. Current volume > previous average volume
    # -------------------------------------------------

    if current["close"] <= current["open"]:
        return None

    volume_sample = previous[-5:]

    avg_volume = sum(
        c["volume"]
        for c in volume_sample
    ) / len(volume_sample)

    if avg_volume <= 0:
        return None

    volume_ratio = (
        current["volume"]
        / avg_volume
    )

    if volume_ratio < VOLUME_MULTIPLIER:
        return None

    return {
        "price": current_price,
        "low": low_24h,
        "first_move": first_move,
        "above_low": above_low,
        "volume_ratio": volume_ratio
    }


# =========================================================
# MAIN SCAN
# =========================================================

def main():

    print("======================================")
    print(" KuCoin 15M RETEST SETUP SCANNER")
    print("======================================")

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print(
            "ERROR: Telegram secrets missing."
        )

        return

    symbols = get_symbols()

    if not symbols:

        print("No symbols found.")
        return

    alerts = 0

    for i, symbol in enumerate(symbols, 1):

        try:

            setup = check_setup(symbol)

            if not setup:
                continue

            price = setup["price"]
            low = setup["low"]
            first_move = setup["first_move"]
            above_low = setup["above_low"]

            message = (
                "🚨 15M RETEST SETUP\n\n"
                f"🪙 {symbol}\n"
                f"📈 First Move: +{first_move:.1f}%\n\n"
                f"📍 24H Low: {low:.8g}\n"
                f"💰 Now: {price:.8g} "
                f"(+{above_low:.1f}%)\n\n"
                "🔥 Buy activity rising"
            )

            print("\n" + message + "\n")

            send_telegram(message)

            alerts += 1

        except Exception as e:

            print(
                f"Error {symbol}: {e}"
            )

        # Avoid API pressure
        time.sleep(0.12)

    print("--------------------------------------")
    print(
        f"Scan complete. Alerts: {alerts}"
    )
    print("--------------------------------------")


# =========================================================
# RUN ONCE AND EXIT
# =========================================================

if __name__ == "__main__":
    main()
