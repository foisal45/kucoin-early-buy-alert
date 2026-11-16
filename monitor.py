
import os
import time
import requests
from datetime import datetime, timedelta
from collections import defaultdict, deque

# Telegram settings
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHANNEL_ID")

if not TOKEN or not CHAT_ID:
    raise ValueError("Telegram Secrets are missing!")

KUCOIN_URL = "https://api.kucoin.com"
TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

SCAN_INTERVAL = 60
RUN_SECONDS = 345 * 60

history = defaultdict(lambda: deque(maxlen=300))
alerted = set()
last_alert = {}
session = requests.Session()


def telegram(message):
    try:
        response = session.post(
            TELEGRAM_URL,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=20
        )
        response.raise_for_status()
        return True
    except Exception as error:
        print("Telegram error:", error)
        return False


def get_market_data():
    url = KUCOIN_URL + "/api/v1/market/allTickers"

    response = session.get(url, timeout=30)
    response.raise_for_status()

    data = response.json()

    if data.get("code") != "200000":
        raise RuntimeError(f"KuCoin API error: {data}")

    return data["data"]["ticker"]


def get_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    result = [ema]

    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
        result.append(ema)

    return result


def get_macd(prices):
    if len(prices) < 40:
        return None

    ema12 = get_ema(prices, 12)
    ema26 = get_ema(prices, 26)

    offset = 12 - 26
    macd = [
        a - b
        for a, b in zip(ema12[offset:], ema26)
    ]

    signal = get_ema(macd, 9)

    if not signal:
        return None

    return macd[-1], signal[-1]


def analyze(symbol, points):
    now = datetime.utcnow()

    recent = [
        price for price, stamp in points
        if now - stamp <= timedelta(minutes=5)
    ]

    trend = [
        price for price, stamp in points
        if now - stamp <= timedelta(hours=4)
    ]

    if len(recent) < 6 or len(trend) < 40:
        return None

    rsi_recent = get_rsi(recent)
    rsi_trend = get_rsi(trend)

    macd_now = get_macd(trend)

    old_points = [
        price for price, stamp in points
        if timedelta(minutes=5) <= now - stamp <= timedelta(minutes=10)
    ]

    macd_old = get_macd(old_points) if len(old_points) >= 40 else None

    if None in (rsi_recent, rsi_trend, macd_now, macd_old):
        return None

    current_price = recent[-1]
    old_price = recent[0]
    trend_price = trend[0]

    if old_price <= 0 or trend_price <= 0:
        return None

    variation_5m = (current_price / old_price - 1) * 100
    variation_4h = (current_price / trend_price - 1) * 100

    return {
        "price": current_price,
        "rsi_recent": rsi_recent,
        "rsi_trend": rsi_trend,
        "macd_now": macd_now,
        "macd_old": macd_old,
        "variation_5m": variation_5m,
        "variation_4h": variation_4h
    }


def monitor():
    start = time.time()
    telegram("🟢 <b>KUCOIN PUMP/DUMP BOT STARTED</b>\n\nMonitoring all USDT pairs.")

    while time.time() - start < RUN_SECONDS:
        try:
            tickers = get_market_data()
            now = datetime.utcnow()

            for ticker in tickers:
                symbol = ticker.get("symbol", "")

                if not symbol.endswith("-USDT"):
                    continue

                if any(x in symbol for x in [
                    "UP-", "DOWN-", "3L-", "3S-", "2L-", "2S-"
                ]):
                    continue

                price = ticker.get("last")

                if not price:
                    continue

                price = float(price)

                if price <= 0:
                    continue

                history[symbol].append((price, now))

            for symbol in list(history.keys()):
                result = analyze(symbol, history[symbol])

                if not result:
                    continue

                rsi = result["rsi_recent"]
                rsi_trend = result["rsi_trend"]
                macd = result["macd_now"]
                old_macd = result["macd_old"]

                current_price = result["price"]
                variation = result["variation_5m"]

                macd_bullish = macd[0] > macd[1]
                old_macd_bullish = old_macd[0] > old_macd[1]

                buy_signal = (
                    rsi > 45
                    and rsi_trend > 45
                    and macd_bullish
                    and old_macd_bullish
                    and variation > 2
                    and result["variation_4h"] > 0
                )

                if buy_signal:
                    last = last_alert.get(symbol, 0)

                    if symbol not in alerted and time.time() - last > 3600:
                        message = (
                            "🟢 <b>STRONG PUMP DETECTED</b>\n\n"
                            f"🧧 <b>{symbol}</b>\n\n"
                            f"💰 Price: {current_price:.10g} USDT\n"
                            f"📈 5m Variation: {variation:.2f}%\n"
                            f"📊 4h Variation: {result['variation_4h']:.2f}%\n"
                            f"📉 RSI: {rsi:.2f}\n"
                            f"📉 Trend RSI: {rsi_trend:.2f}\n\n"
                            "MACD confirms bullish momentum."
                        )

                        if telegram(message):
                            alerted.add(symbol)
                            last_alert[symbol] = time.time()

                elif symbol in alerted:
                    dump_signal = (
                        rsi > 90
                        and (
                            variation < -3
                            or (
                                not macd_bullish
                                and not old_macd_bullish
                            )
                        )
                    )

                    if dump_signal:
                        message = (
                            "🔴 <b>MOMENTUM DUMPING BELOW</b>\n\n"
                            f"🧧 <b>{symbol}</b>\n\n"
                            f"💰 Price: {current_price:.10g} USDT\n"
                            f"📉 Variation: {variation:.2f}%\n"
                            f"RSI: {rsi:.2f}\n\n"
                            "Momentum weakening. Consider risk management."
                        )

                        if telegram(message):
                            alerted.discard(symbol)

            print(
                f"{datetime.utcnow()} | "
                f"Scanned {len(history)} USDT pairs"
            )

        except Exception as error:
            print("Monitoring error:", error)

        time.sleep(SCAN_INTERVAL)

    telegram("⏹ <b>BOT SESSION FINISHED</b>\nGitHub Actions session ended.")


if __name__ == "__main__":
    monitor()
