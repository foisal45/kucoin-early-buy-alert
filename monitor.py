import os
import json
import requests
from datetime import datetime, timezone

API = "https://api.kucoin.com"
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
STATE_FILE = "alert_state.json"

session = requests.Session()


def get(path, params=None):
    try:
        r = session.get(API + path, params=params, timeout=15)
        data = r.json()

        if data.get("code") == "200000":
            return data.get("data")

    except Exception as e:
        print("API error:", e)

    return None


def telegram(message):
    if not TOKEN or not CHAT_ID:
        return False

    url = "https://api.telegram.org/bot" + TOKEN + "/sendMessage"

    try:
        r = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=15
        )

        return r.ok

    except Exception:
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
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def price_text(p):
    if p >= 100:
        return "{:.2f}".format(p)
    if p >= 1:
        return "{:.4f}".format(p)
    if p >= 0.01:
        return "{:.6f}".format(p)
    return "{:.10f}".format(p)


def main():

    print("KUCOIN 50% MOVE / 24H LOW RETEST SCANNER")

    state = load_state()

    symbols = get("/api/v2/symbols")

    if not symbols:
        print("No symbols found.")
        return

    for item in symbols:

        if item.get("quoteCurrency") != "USDT":
            continue

        if not item.get("enableTrading"):
            continue

        symbol = item.get("symbol")

        if not symbol:
            continue

        ticker = get(
            "/api/v1/market/stats",
            {"symbol": symbol}
        )

        if not ticker:
            continue

        try:
            current = float(ticker["last"])
            low24 = float(ticker["low"])
            high24 = float(ticker["high"])
        except Exception:
            continue

        if low24 <= 0:
            continue

        above_low = ((current - low24) / low24) * 100
        previous_move = ((high24 - low24) / low24) * 100

        # Must have moved at least 50% from 24H low
        if previous_move < 50:
            continue

        # Alert when price comes back to +10% or below
        if above_low > 10:
            continue

        setup_id = symbol + "_" + str(round(low24, 12))

        if setup_id in state:
            continue

        message = (
            "🟢 2ND-MOVE RETEST ALERT\n\n"
            "🪙 " + symbol + "\n"
            "💰 Current Price: " + price_text(current) + "\n"
            "📉 24H Low: " + price_text(low24) + "\n"
            "📊 Above 24H Low: "
            + "{:.2f}".format(above_low)
            + "%\n\n"
            "🚀 Previous 24H Move: +"
            + "{:.1f}".format(previous_move)
            + "%\n"
            "📈 24H High: " + price_text(high24) + "\n\n"
            "🔄 Strong move → Retrace → Near 24H Low\n"
            "🎯 Price is now at or below +10% from 24H Low\n\n"
            "⚠️ Check chart before entry."
        )

        if telegram(message):

            state[setup_id] = {
                "symbol": symbol,
                "low": low24,
                "alerted_at": datetime.now(
                    timezone.utc
                ).isoformat()
            }

            save_state(state)

            print("ALERT:", symbol)

    save_state(state)
    print("Scan completed.")


if __name__ == "__main__":
    main()
