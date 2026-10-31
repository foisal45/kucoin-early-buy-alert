
import os
import json
import time
import requests
from datetime import datetime, timezone
from statistics import mean

BASE = "https://api.kucoin.com"
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

STATE_FILE = "state.json"
MAX_COINS = 40
MAX_PRICE_CHANGE = 15

session = requests.Session()
session.headers.update({
    "User-Agent": "KuCoin-Smart-Scanner/2.0"
})


def api(path, params=None):
    try:
        r = session.get(
            BASE + path,
            params=params,
            timeout=15
        )

        if r.status_code != 200:
            print(
                "API error:",
                path,
                r.status_code,
                r.text[:300]
            )
            return None

        data = r.json()

        if data.get("code") == "200000":
            return data.get("data")

        print("KuCoin API response error:", data)
        return None

    except Exception as e:
        print("API error:", path, e)
        return None


def telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_TOKEN or CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        r = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=15
        )

        result = r.json()

        if not result.get("ok"):
            print("Telegram response error:", result)
            return False

        return True

    except Exception as e:
        print("Telegram error:", e)
        return False


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "alerts": [],
            "daily_report": ""
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def deposit_off_currencies():
    currencies = api("/api/v3/currencies")
    result = set()

    if not currencies:
        return result

    for coin in currencies:
        chains = coin.get("chains") or []

        if not chains:
            continue

        if all(
            chain.get("isDepositEnabled") is False
            for chain in chains
        ):
            result.add(coin.get("currency"))

    print("Deposit-OFF currencies:", len(result))
    return result


def get_tickers():
    data = api("/api/v1/market/allTickers")

    if not data:
        return []

    return data.get("ticker", [])


def ema(values, period):
    if len(values) < period:
        return None

    result = values[0]
    multiplier = 2 / (period + 1)

    for value in values[1:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = mean(gains[-period:])
    avg_loss = mean(losses[-period:])

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def get_candles(symbol):
    data = api(
        "/api/v1/market/candles",
        {
            "symbol": symbol,
            "type": "1hour"
        }
    )

    if not data:
        return []

    candles = sorted(
        data,
        key=lambda x: int(x[0])
    )

    return candles


def analyze(symbol):
    candles = get_candles(symbol)

    if len(candles) < 55:
        return None

    # Ignore currently forming candle
    candles = candles[:-1]

    closes = [float(c[2]) for c in candles]
    highs = [float(c[3]) for c in candles]
    lows = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]

    price = closes[-1]
    old_price = closes[-4]

    if old_price <= 0:
        return None

    change = (
        (price - old_price) / old_price
    ) * 100

    if change < -35 or change >= MAX_PRICE_CHANGE:
        return None

    recent_volume = mean(volumes[-3:])
    previous_volume = mean(volumes[-6:-3])

    if previous_volume <= 0:
        return None

    volume_ratio = recent_volume / previous_volume

    ema20 = ema(closes[-60:], 20)
    ema50 = ema(closes[-100:], 50)

    current_rsi = rsi(closes)

    ema12 = ema(closes[-60:], 12)
    ema26 = ema(closes[-60:], 26)

    macd_bullish = (
        ema12 is not None
        and ema26 is not None
        and ema12 > ema26
    )

    # Recent trades
    trades = api(
        "/api/v1/market/histories",
        {"symbol": symbol}
    ) or []

    buy_volume = 0
    sell_volume = 0

    for trade in trades:
        try:
            size = float(trade.get("size", 0))

            if trade.get("side") == "buy":
                buy_volume += size
            else:
                sell_volume += size

        except Exception:
            continue

    total = buy_volume + sell_volume

    buy_ratio = (
        buy_volume / total
        if total > 0 else 0
    )

    # FIXED ORDER BOOK ENDPOINT
    orderbook = api(
        "/api/v1/market/orderbook/level2_20",
        {"symbol": symbol}
    )

    bid_ratio = 0

    if orderbook:
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        bid_value = sum(
            float(x[0]) * float(x[1])
            for x in bids
        )

        ask_value = sum(
            float(x[0]) * float(x[1])
            for x in asks
        )

        total_book = bid_value + ask_value

        if total_book > 0:
            bid_ratio = bid_value / total_book

    score = 0
    reasons = []

    if volume_ratio >= 1.5:
        score += 20
        reasons.append("Volume surge")

    if change <= 0:
        score += 15
        reasons.append("Price not rising")

    if buy_ratio >= 0.52:
        score += 15
        reasons.append("Buy pressure")

    if bid_ratio >= 0.50:
        score += 10
        reasons.append("Bid support")

    if ema20 and ema50 and ema20 > ema50:
        score += 15
        reasons.append("EMA bullish")

    if current_rsi and 50 <= current_rsi <= 72:
        score += 10
        reasons.append("RSI bullish")

    if macd_bullish:
        score += 10
        reasons.append("MACD bullish")

    if ema20 and price > ema20:
        score += 5
        reasons.append("Above EMA20")

    signal = None

    if (
        volume_ratio >= 1.5
        and -8 <= change <= 0
        and buy_ratio >= 0.52
        and bid_ratio >= 0.50
    ):
        signal = "ACCUMULATION WATCH"

    elif (
        ema20
        and ema50
        and ema20 > ema50
        and price > ema20
        and macd_bullish
        and current_rsi
        and 50 <= current_rsi <= 72
        and buy_ratio >= 0.58
        and bid_ratio >= 0.55
        and volume_ratio >= 1.15
    ):
        signal = "BULLISH"

    if not signal:
        return None

    return {
        "symbol": symbol,
        "price": price,
        "change": change,
        "volume_ratio": volume_ratio,
        "rsi": current_rsi,
        "buy_ratio": buy_ratio,
        "bid_ratio": bid_ratio,
        "score": score,
        "signal": signal,
        "reasons": reasons
    }


def news_alert(state):
    data = api("/api/v3/announcements")

    if not data:
        print("News API unavailable")
        return

    items = (
        data.get("items", [])
        if isinstance(data, dict)
        else []
    )

    positive = [
        "listing",
        "launch",
        "partnership",
        "upgrade",
        "mainnet",
        "integration",
        "support"
    ]

    negative = [
        "delist",
        "suspend",
        "maintenance",
        "withdrawal suspended",
        "deposit suspended"
    ]

    for item in items[:20]:
        title = item.get("title", "")
        url = item.get("url", "")

        key = str(
            item.get("annId") or title
        )

        if key in state["alerts"]:
            continue

        lower = title.lower()

        if any(word in lower for word in positive):
            sent = telegram(
                "📰 <b>KuCoin Positive News Watch</b>\n\n"
                f"{title}\n{url}"
            )

            if sent:
                state["alerts"].append(key)

        elif any(word in lower for word in negative):
            sent = telegram(
                "⚠️ <b>KuCoin Important Announcement</b>\n\n"
                f"{title}\n{url}"
            )

            if sent:
                state["alerts"].append(key)


def daily_report(results, state):
    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    if state.get("daily_report") == today:
        return

    top = sorted(
        results,
        key=lambda x: x["score"],
        reverse=True
    )[:10]

    if not top:
        message = (
            "📊 <b>Daily Deposit-OFF Watchlist</b>\n\n"
            "আজ কোনো শক্তিশালী signal পাওয়া যায়নি।"
        )

    else:
        message = (
            "📊 <b>Daily Deposit-OFF Watchlist</b>\n\n"
        )

        for item in top:
            message += (
                f"• <b>{item['symbol']}</b>\n"
                f"Score: {item['score']}/100\n"
                f"Price change: {item['change']:.2f}%\n"
                f"Volume: {item['volume_ratio']:.2f}x\n"
                f"RSI: {item['rsi']:.1f}\n\n"
            )

    if telegram(message):
        state["daily_report"] = today


def main():
    state = load_state()

    print("Starting KuCoin Smart Scanner...")

    if not TOKEN or not CHAT_ID:
        print("Telegram secrets missing!")
        return

    off_coins = deposit_off_currencies()
    tickers = get_tickers()

    if not off_coins or not tickers:
        print("Currency or ticker data unavailable.")
        return

    candidates = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        if not symbol.endswith("-USDT"):
            continue

        coin = symbol.split("-")[0]

        if coin not in off_coins:
            continue

        try:
            change = (
                float(ticker.get("changeRate", 0)) * 100
            )

            volume = float(
                ticker.get("volValue", 0)
            )

            if (
                -8 <= change < MAX_PRICE_CHANGE
                and volume > 0
            ):
                candidates.append(
                    (symbol, volume)
                )

        except Exception:
            continue

    candidates.sort(
        key=lambda x: x[1],
        reverse=True
    )

    candidates = candidates[:MAX_COINS]

    print("Candidates:", len(candidates))

    results = []

    for symbol, _ in candidates:
        result = analyze(symbol)

        if not result:
            continue

        results.append(result)

        key = (
            result["symbol"]
            + "_"
            + result["signal"]
            + "_"
            + str(datetime.now(timezone.utc).date())
        )

        if key not in state["alerts"]:
            message = (
                f"🚨 <b>{result['signal']}</b>\n\n"
                f"Coin: <b>{result['symbol']}</b>\n"
                f"Price: {result['price']}\n"
                f"3h Change: {result['change']:.2f}%\n"
                f"Volume: {result['volume_ratio']:.2f}x\n"
                f"RSI: {result['rsi']:.1f}\n"
                f"Buy ratio: {result['buy_ratio']:.2f}\n"
                f"Bid ratio: {result['bid_ratio']:.2f}\n"
                f"Score: {result['score']}/100\n\n"
                f"Reasons: {', '.join(result['reasons'])}\n\n"
                "⚠️ Research alert, not a guaranteed buy signal."
            )

            if telegram(message):
                state["alerts"].append(key)

        time.sleep(0.25)

    news_alert(state)
    daily_report(results, state)

    state["alerts"] = state["alerts"][-2000:]

    save_state(state)

    print("Scanner finished.")


if __name__ == "__main__":
    main()
