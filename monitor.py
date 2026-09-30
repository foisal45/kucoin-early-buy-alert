import os
import json
import time
import requests
from datetime import datetime, timezone

API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

STATE_FILE = "alert_state.json"

LOOKBACK = 20
MIN_DAILY_MOVE = 8.0
MIN_BREAKOUT = 3.0
MIN_PULLBACK = 4.0
MAX_PULLBACK = 35.0
RETEST_DISTANCE = 4.0
MIN_TURNOVER = 50000

session = requests.Session()
session.headers.update({
    "User-Agent": "KuCoin-Daily-Retest-Scanner"
})


def api_get(path, params=None):
    try:
        r = session.get(
            API + path,
            params=params,
            timeout=20
        )
        r.raise_for_status()

        data = r.json()

        if data.get("code") != "200000":
            return None

        return data.get("data")

    except Exception as e:
        print("API error:", e)
        return None


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets are missing.")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": text
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
        print("Telegram error:", e)
        return False


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


def get_symbols():
    data = api_get("/api/v2/symbols")

    if not data:
        return []

    result = []

    for item in data:

        if item.get("quoteCurrency") != "USDT":
            continue

        if not item.get("enableTrading"):
            continue

        symbol = item.get("symbol")

        if symbol:
            result.append(symbol)

    return result


def get_daily_candles(symbol):
    data = api_get(
        "/api/v1/market/candles",
        {
            "symbol": symbol,
            "type": "1day"
        }
    )

    if not data:
        return []

    result = []

    for row in data:

        try:
            result.append({
                "time": int(row[0]),
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
                "turnover": float(row[6])
            })

        except Exception:
            continue

    result.sort(key=lambda x: x["time"])

    return result


def get_current_price(symbol):
    data = api_get(
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


def find_setup(symbol, candles, current_price):

    if len(candles) < LOOKBACK + 10:
        return None

    completed = candles[:-1]

    if len(completed) < LOOKBACK + 8:
        return None

    candidates = []

    start = max(
        LOOKBACK,
        len(completed) - 30
    )

    for i in range(start, len(completed) - 3):

        candle = completed[i]

        previous = completed[i - LOOKBACK:i]

        previous_high = max(
            x["high"] for x in previous
        )

        if previous_high <= 0:
            continue

        daily_move = (
            (candle["close"] - candle["open"])
            / candle["open"]
        ) * 100

        breakout = (
            (candle["close"] - previous_high)
            / previous_high
        ) * 100

        if daily_move < MIN_DAILY_MOVE:
            continue

        if breakout < MIN_BREAKOUT:
            continue

        if candle["turnover"] < MIN_TURNOVER:
            continue

        candidates.append({
            "index": i,
            "zone": previous_high,
            "candle": candle
        })

    if not candidates:
        return None

    setup = candidates[-1]

    breakout_index = setup["index"]
    zone = setup["zone"]
    breakout_candle = setup["candle"]

    after = completed[breakout_index + 1:]

    if len(after) < 2:
        return None

    post_high = max(
        x["high"] for x in after
    )

    if post_high <= zone:
        return None

    pullback = (
        (post_high - current_price)
        / post_high
    ) * 100

    if pullback < MIN_PULLBACK:
        return None

    if pullback > MAX_PULLBACK:
        return None

    distance = (
        (current_price - zone)
        / zone
    ) * 100

    if abs(distance) > RETEST_DISTANCE:
        return None

    moved_away = False

    for candle in after:

        if candle["high"] >= zone * 1.08:
            moved_away = True
            break

    if not moved_away:
        return None

    touched = False

    for candle in completed[-5:]:

        if (
            candle["low"] <= zone * 1.04
            and candle["high"] >= zone * 0.96
        ):
            touched = True
            break

    if not touched:
        return None

    if current_price < zone * 0.96:
        return None

    daily_move = (
        (breakout_candle["close"] - breakout_candle["open"])
        / breakout_candle["open"]
    ) * 100

    breakout = (
        (breakout_candle["close"] - zone)
        / zone
    ) * 100

    breakout_date = datetime.fromtimestamp(
        breakout_candle["time"],
        tz=timezone.utc
    ).strftime("%Y-%m-%d")

    return {
        "symbol": symbol,
        "price": current_price,
        "zone": zone,
        "post_high": post_high,
        "pullback": pullback,
        "distance": distance,
        "daily_move": daily_move,
        "breakout": breakout,
        "date": breakout_date
    }


def format_price(price):

    if price >= 100:
        return "{:.2f}".format(price)

    if price >= 1:
        return "{:.4f}".format(price)

    if price >= 0.01:
        return "{:.6f}".format(price)

    if price >= 0.0001:
        return "{:.8f}".format(price)

    return "{:.10f}".format(price)


def main():

    print("========================================")
    print("KUCOIN DAILY 2ND-MOVE RETEST SCANNER")
    print("========================================")

    state = load_state()

    symbols = get_symbols()

    print("USDT pairs:", len(symbols))

    alerts = 0

    for symbol in symbols:

        try:

            candles = get_daily_candles(symbol)

            if not candles:
                continue

            price = get_current_price(symbol)

            if not price:
                continue

            setup = find_setup(
                symbol,
                candles,
                price
            )

            if not setup:
                continue

            setup_id = (
                symbol
                + "_"
                + setup["date"]
                + "_"
                + str(round(setup["zone"], 10))
            )

            if setup_id in state:
                continue

            message = (
                "KUCOIN 2ND-MOVE SETUP\n"
                "\n"
                "Coin: " + setup["symbol"] + "\n"
                "Current Price: " + format_price(setup["price"]) + "\n"
                "\n"
                "Previous Daily Move: +"
                + "{:.1f}".format(setup["daily_move"])
                + "%\n"
                "Breakout: +"
                + "{:.1f}".format(setup["breakout"])
                + "%\n"
                "Breakout Date: "
                + setup["date"]
                + "\n"
                "\n"
                "Retest Zone: "
                + format_price(setup["zone"])
                + "\n"
                "Distance From Zone: "
                + "{:+.2f}".format(setup["distance"])
                + "%\n"
                "Pullback From High: "
                + "{:.1f}".format(setup["pullback"])
                + "%\n"
                "Previous High: "
                + format_price(setup["post_high"])
                + "\n"
                "\n"
                "Setup: Previous breakout/support zone "
                "is being retested after a strong Daily move.\n"
                "\n"
                "Wait for confirmation before entry."
            )

            if send_telegram(message):

                state[setup_id] = {
                    "symbol": symbol,
                    "date": setup["date"],
                    "alerted_at": datetime.now(
                        timezone.utc
                    ).isoformat()
                }

                save_state(state)

                alerts += 1

            time.sleep(0.15)

        except Exception as e:

            print(
                "Error:",
                symbol,
                e
            )

    save_state(state)

    print("========================================")
    print("Scan completed.")
    print("Alerts:", alerts)
    print("========================================")


if __name__ == "__main__":
    main()
