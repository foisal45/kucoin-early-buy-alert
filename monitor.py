import os
import time
import json
import requests

KUCOIN_API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ==============================
# SETTINGS
# ==============================

MIN_FIRST_MOVE = 20.0       # First move from 24H low
MAX_ABOVE_LOW = 15.0        # Retest must be within +15% of 24H low
MIN_RETRACE = 12.0          # Minimum pullback from first high

VOLUME_MULTIPLIER = 1.10    # Retest candle volume
MIN_LISTED_DAYS = 30

STATE_FILE = "alerts_state.json"


# ==============================
# TELEGRAM
# ==============================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets missing")
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

        return r.status_code == 200

    except Exception as e:

        print("Telegram error:", e)
        return False


# ==============================
# STATE
# ==============================

def load_state():

    try:

        if os.path.exists(STATE_FILE):

            with open(
                STATE_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                return json.load(f)

    except Exception as e:

        print("State read error:", e)

    return {}


def save_state(state):

    try:

        with open(
            STATE_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                state,
                f,
                indent=2
            )

    except Exception as e:

        print("State save error:", e)


# ==============================
# SYMBOLS
# ==============================

def get_symbols():

    url = f"{KUCOIN_API}/api/v2/symbols"

    try:

        r = requests.get(
            url,
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

    symbols = []

    for item in data:

        try:

            if item.get(
                "quoteCurrency"
            ) != "USDT":

                continue

            if not item.get(
                "enableTrading"
            ):

                continue

            list_at = item.get(
                "listAt"
            )

            if list_at:

                age_days = (
                    now - int(list_at)
                ) / 1000 / 86400

                if age_days < MIN_LISTED_DAYS:
                    continue

            symbols.append(
                item["symbol"]
            )

        except Exception:

            continue

    print(
        "USDT coins:",
        len(symbols)
    )

    return symbols


# ==============================
# CANDLES
# ==============================

def get_candles(symbol):

    end = int(
        time.time()
    )

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

                pass

        candles.sort(
            key=lambda x: x["time"]
        )

        return candles

    except Exception as e:

        print(
            f"Candle error {symbol}:",
            e
        )

        return []


# ==============================
# SETUP DETECTOR
# ==============================

def check_setup(symbol):

    candles = get_candles(
        symbol
    )

    if len(candles) < 30:
        return None

    # Ignore currently forming candle
    candles = candles[:-1]

    if len(candles) < 25:
        return None

    current = candles[-1]

    # ==============================
    # 24H LOW
    # ==============================

    low_24h = min(
        c["low"]
        for c in candles
    )

    if low_24h <= 0:
        return None

    # ==============================
    # FIRST MOVE
    # ==============================

    # Look for strongest high
    # before the latest few candles

    search = candles[:-3]

    if not search:
        return None

    peak_candle = max(
        search,
        key=lambda c: c["high"]
    )

    peak = peak_candle["high"]

    peak_index = search.index(
        peak_candle
    )

    # Need some candles after peak
    if (
        len(search)
        - peak_index
        < 3
    ):
        return None

    first_move = (
        (peak - low_24h)
        / low_24h
    ) * 100

    if first_move < MIN_FIRST_MOVE:
        return None

    # ==============================
    # CURRENT PRICE
    # ==============================

    price = current["close"]

    if price <= 0:
        return None

    above_low = (
        (price - low_24h)
        / low_24h
    ) * 100

    # Must be close to 24H low
    if above_low < 0:
        return None

    if above_low > MAX_ABOVE_LOW:
        return None

    # ==============================
    # RETRACE
    # ==============================

    retrace = (
        (peak - price)
        / peak
    ) * 100

    if retrace < MIN_RETRACE:
        return None

    # ==============================
    # BULLISH TURN
    # ==============================

    if current["close"] <= current["open"]:
        return None

    # ==============================
    # VOLUME
    # ==============================

    previous = candles[-6:-1]

    if len(previous) < 3:
        return None

    avg_volume = (
        sum(
            c["volume"]
            for c in previous
        )
        /
        len(previous)
    )

    if avg_volume <= 0:
        return None

    volume_ratio = (
        current["volume"]
        /
        avg_volume
    )

    if volume_ratio < VOLUME_MULTIPLIER:
        return None

    # ==============================
    # SETUP FOUND
    # ==============================

    return {
        "price": price,
        "low": low_24h,
        "peak": peak,
        "first_move": first_move,
        "above_low": above_low,
        "retrace": retrace,
        "volume_ratio": volume_ratio,
        "candle_time": current["time"]
    }


# ==============================
# MAIN
# ==============================

def main():

    print(
        "================================"
    )

    print(
        " KUCOIN 15M RETEST SCANNER"
    )

    print(
        "================================"
    )

    if (
        not TELEGRAM_TOKEN
        or not CHAT_ID
    ):

        print(
            "Telegram secrets missing"
        )

        return

    state = load_state()

    symbols = get_symbols()

    if not symbols:

        return

    alerts = 0

    for symbol in symbols:

        try:

            setup = check_setup(
                symbol
            )

            if not setup:
                continue

            # ==========================
            # DUPLICATE PROTECTION
            # ==========================

            setup_key = (
                f"{symbol}_"
                f"{setup['candle_time']}"
            )

            if state.get(
                symbol
            ) == setup_key:

                continue

            # ==========================
            # MESSAGE
            # ==========================

            message = (
                "🚨 15M RETEST SETUP\n\n"
                f"🪙 {symbol}\n"
                f"📈 First Move: "
                f"+{setup['first_move']:.1f}%\n"
                f"📍 24H Low: "
                f"{setup['low']:.8g}\n"
                f"💰 Now: "
                f"{setup['price']:.8g} "
                f"(+{setup['above_low']:.1f}%)\n"
                f"🔥 Volume: "
                f"{setup['volume_ratio']:.1f}x"
            )

            print(
                "\n",
                message,
                "\n"
            )

            if send_telegram(
                message
            ):

                state[symbol] = setup_key

                save_state(
                    state
                )

                alerts += 1

        except Exception as e:

            print(
                f"{symbol} error:",
                e
            )

        time.sleep(
            0.12
        )

    print(
        "================================"
    )

    print(
        f"Alerts sent: {alerts}"
    )

    print(
        "================================"
    )


if __name__ == "__main__":

    main()
