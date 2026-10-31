
import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://api.kucoin.com"
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
STATE_FILE = "state.json"

HEADERS = {"User-Agent": "KuCoin-Smart-Scanner/1.0"}
BD_TZ = timezone(timedelta(hours=6))

# ---------------- CONFIG ----------------

MAX_PRICE_CHANGE = 15
MIN_VOLUME_INCREASE = 1.5
MAX_CANDIDATES = 35
TOP_SIGNALS = 8

POSITIVE_WORDS = [
    "partnership", "integration", "launch", "mainnet",
    "upgrade", "adoption", "listing", "funding",
    "investment", "burn", "ecosystem", "support",
    "expansion", "milestone", "approval", "bullish"
]

NEGATIVE_WORDS = [
    "hack", "exploit", "security incident", "suspend",
    "delist", "delisting", "lawsuit", "investigation",
    "breach", "vulnerability", "warning", "scam"
]

# ---------------- API ----------------

def api(path, params=None):
    try:
        r = requests.get(
            BASE + path,
            params=params,
            headers=HEADERS,
            timeout=15
        )
        r.raise_for_status()
        data = r.json()

        if data.get("code") != "200000":
            return None

        return data.get("data")

    except Exception as e:
        print("API ERROR:", path, str(e))
        return None


def telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram secrets missing")
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=15
        ).raise_for_status()
    except Exception as e:
        print("Telegram error:", e)


# ---------------- STATE ----------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "alerts": {},
            "daily_date": "",
            "daily_sent": False
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------- DEPOSIT OFF ----------------

def get_deposit_off_coins():
    data = api("/api/v3/currencies")

    if not data:
        return set()

    result = set()

    for coin in data:
        currency = coin.get("currency")
        chains = coin.get("chains") or []

        # Every known network must be deposit OFF.
        # Missing network information is NOT treated as OFF.
        if not currency or not chains:
            continue

        if all(c.get("isDepositEnabled") is False for c in chains):
            result.add(currency)

    return result


# ---------------- MARKET DATA ----------------

def get_market():
    symbols = api("/api/v2/symbols") or []
    tickers = api("/api/v1/market/allTickers") or {}

    ticker_list = tickers.get("ticker", [])

    symbol_map = {
        x["symbol"]: x
        for x in symbols
        if x.get("enableTrading") is True
        and x.get("quoteCurrency") == "USDT"
        and not x.get("st", False)
    }

    ticker_map = {
        x["symbol"]: x
        for x in ticker_list
        if x.get("symbol") in symbol_map
    }

    return symbol_map, ticker_map


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

    # KuCoin returns newest candle first.
    return list(reversed(data))


def get_orderbook(symbol):
    data = api(
        "/api/v3/market/orderbook/level2_20",
        {"symbol": symbol}
    )

    if not data:
        return None

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    bid_value = sum(
        float(x[0]) * float(x[1]) for x in bids
    )

    ask_value = sum(
        float(x[0]) * float(x[1]) for x in asks
    )

    total = bid_value + ask_value

    if total <= 0:
        return None

    return {
        "bid": bid_value,
        "ask": ask_value,
        "ratio": bid_value / total
    }


def get_recent_trade_pressure(symbol):
    data = api(
        "/api/v1/market/histories",
        {"symbol": symbol}
    )

    if not data:
        return None

    buy = 0
    sell = 0

    for trade in data:
        value = float(trade.get("price", 0)) * float(
            trade.get("size", 0)
        )

        if trade.get("side") == "buy":
            buy += value
        elif trade.get("side") == "sell":
            sell += value

    total = buy + sell

    if total <= 0:
        return None

    return {
        "buy": buy,
        "sell": sell,
        "ratio": buy / total
    }


# ---------------- INDICATORS ----------------

def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd_bullish(values):
    if len(values) < 35:
        return False

    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return False

    # Current MACD above zero is not required:
    # the trend and RSI filters also matter.
    return fast > slow


# ---------------- ANALYSIS ----------------

def analyze(symbol, ticker):
    try:
        change = float(ticker.get("changeRate", 0)) * 100
        price = float(ticker.get("last", 0))
        volume24 = float(ticker.get("volValue", 0))

        if price <= 0 or volume24 <= 0:
            return None

        if change >= MAX_PRICE_CHANGE or change < -35:
            return None

        candles = get_candles(symbol)

        if len(candles) < 55:
            return None

        # Candle structure:
        # [time, open, close, high, low, volume, turnover]

        closes = [float(x[2]) for x in candles]
        highs = [float(x[3]) for x in candles]
        lows = [float(x[4]) for x in candles]
        volumes = [float(x[5]) for x in candles]
        turnovers = [float(x[6]) for x in candles]

        # Exclude the currently forming candle.
        closes = closes[:-1]
        highs = highs[:-1]
        lows = lows[:-1]
        volumes = volumes[:-1]
        turnovers = turnovers[:-1]

        if len(closes) < 50:
            return None

        # Last 3 closed candles vs previous 3.
        recent_vol = sum(turnovers[-3:])
        previous_vol = sum(turnovers[-6:-3])

        if previous_vol <= 0:
            return None

        volume_ratio = recent_vol / previous_vol

        # Last 3 hours vs previous 3 hours.
        recent_price_change = (
            closes[-1] / closes[-4] - 1
        ) * 100

        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        rsi_value = rsi(closes)

        if ema20 is None or ema50 is None or rsi_value is None:
            return None

        macd_ok = macd_bullish(closes)

        orderbook = get_orderbook(symbol)
        trades = get_recent_trade_pressure(symbol)

        if not orderbook or not trades:
            return None

        buy_ratio = trades["ratio"]
        book_ratio = orderbook["ratio"]

        # Support / resistance from recent candles.
        support = min(lows[-24:])
        resistance = max(highs[-24:])

        # Approximate VWAP from recent 24 candles.
        v_volume = sum(volumes[-24:])
        v_turnover = sum(turnovers[-24:])

        vwap = (
            v_turnover / v_volume
            if v_volume > 0 else price
        )

        # -------- Accumulation Watch --------

        accumulation = (
            volume_ratio >= MIN_VOLUME_INCREASE
            and recent_price_change <= 0
            and recent_price_change >= -8
            and buy_ratio >= 0.52
            and book_ratio >= 0.50
        )

        # -------- Bullish Setup --------

        bullish = (
            ema20 > ema50
            and closes[-1] > ema20
            and macd_ok
            and 50 <= rsi_value <= 72
            and buy_ratio >= 0.58
            and book_ratio >= 0.55
            and volume_ratio >= 1.15
            and price >= vwap
        )

        # -------- Score --------

        score = 0

        if volume_ratio >= 1.5:
            score += 20
        if volume_ratio >= 2:
            score += 10
        if ema20 > ema50:
            score += 15
        if macd_ok:
            score += 10
        if 50 <= rsi_value <= 68:
            score += 10
        if buy_ratio >= 0.60:
            score += 15
        if book_ratio >= 0.60:
            score += 10
        if price >= vwap:
            score += 10

        if accumulation:
            signal = "ACCUMULATION WATCH"
        elif bullish:
            signal = "BULLISH"
        else:
            return None

        # ATR-like simple stop estimate.
        ranges = [
            highs[i] - lows[i]
            for i in range(-14, 0)
        ]

        atr = sum(ranges) / len(ranges)

        entry_low = max(support, price - atr * 0.5)
        entry_high = price + atr * 0.2

        stop = max(
            support - atr * 0.5,
            price * 0.90
        )

        target1 = price + atr * 1.5
        target2 = price + atr * 2.5

        return {
            "symbol": symbol,
            "price": price,
            "change": change,
            "volume_ratio": volume_ratio,
            "recent_change": recent_price_change,
            "rsi": rsi_value,
            "ema20": ema20,
            "ema50": ema50,
            "buy_ratio": buy_ratio,
            "book_ratio": book_ratio,
            "vwap": vwap,
            "support": support,
            "resistance": resistance,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop": stop,
            "target1": target1,
            "target2": target2,
            "score": score,
            "signal": signal
        }

    except Exception as e:
        print("ANALYSIS ERROR:", symbol, str(e))
        return None


# ---------------- NEWS ----------------

def get_news():
    data = api(
        "/api/v3/announcements",
        {
            "currentPage": 1,
            "pageSize": 100,
            "annType": "latest-announcements",
            "lang": "en_US"
        }
    )

    if not data:
        return []

    return data.get("items", [])


def classify_news(news, symbols):
    results = []

    for item in news:
        title = item.get("annTitle", "")
        lower = title.lower()

        negative = any(x in lower for x in NEGATIVE_WORDS)
        positive = any(x in lower for x in POSITIVE_WORDS)

        if negative:
            sentiment = "NEGATIVE"
        elif positive:
            sentiment = "POSITIVE"
        else:
            continue

        matched = []

        for symbol in symbols:
            base = symbol.split("-")[0].lower()

            if len(base) >= 3 and base in lower:
                matched.append(symbol)

        results.append({
            "title": title,
            "sentiment": sentiment,
            "coins": matched,
            "url": item.get("annUrl", "")
        })

    return results


# ---------------- MESSAGES ----------------

def fmt_price(x):
    if x >= 100:
        return f"{x:.3f}"
    if x >= 1:
        return f"{x:.5f}"
    if x >= 0.0001:
        return f"{x:.7f}"
    return f"{x:.10f}"


def signal_message(x, deposit_off):
    coin = x["symbol"]
    is_off = coin.split("-")[0] in deposit_off

    return (
        f"🚨 <b>{x['signal']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🪙 <b>{coin}</b>\n"
        f"💰 Price: {fmt_price(x['price'])}\n"
        f"📊 24h Change: {x['change']:.2f}%\n"
        f"📈 Volume increase: {x['volume_ratio']:.2f}x\n"
        f"⏱ 3h Price change: {x['recent_change']:.2f}%\n"
        f"🟢 Trade Buy Pressure: {x['buy_ratio']*100:.1f}%\n"
        f"📗 Orderbook Bid Ratio: {x['book_ratio']*100:.1f}%\n"
        f"📉 RSI: {x['rsi']:.1f}\n"
        f"EMA20: {fmt_price(x['ema20'])}\n"
        f"EMA50: {fmt_price(x['ema50'])}\n"
        f"VWAP: {fmt_price(x['vwap'])}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎯 Entry zone: {fmt_price(x['entry_low'])} - "
        f"{fmt_price(x['entry_high'])}\n"
        f"🛑 Stop Loss: {fmt_price(x['stop'])}\n"
        f"🎯 Target 1: {fmt_price(x['target1'])}\n"
        f"🎯 Target 2: {fmt_price(x['target2'])}\n"
        f"Support: {fmt_price(x['support'])}\n"
        f"Resistance: {fmt_price(x['resistance'])}\n"
        f"⭐ Score: {x['score']}/100\n"
        f"🏦 All-network Deposit OFF: "
        f"{'YES' if is_off else 'NO'}\n"
        f"⚠️ Research signal, not guaranteed entry."
    )


# ---------------- SCAN ----------------

def main():
    state = load_state()

    print("Starting KuCoin Smart Scanner...")

    deposit_off = get_deposit_off_coins()
    symbols, tickers = get_market()

    if not deposit_off or not symbols or not tickers:
        print("Market/deposit data unavailable. Stop safely.")
        return

    # Only existing USDT spot pairs.
    candidates = []

    for symbol, info in symbols.items():
        base = info.get("baseCurrency")

        if base not in deposit_off:
            continue

        ticker = tickers.get(symbol)

        if not ticker:
            continue

        change = float(ticker.get("changeRate", 0)) * 100
        vol = float(ticker.get("volValue", 0))

        if 0 <= change < MAX_PRICE_CHANGE and vol > 0:
            candidates.append((symbol, ticker))

    candidates.sort(
        key=lambda x: float(x[1].get("volValue", 0)),
        reverse=True
    )

    candidates = candidates[:MAX_CANDIDATES]

    print("Deposit OFF candidates:", len(candidates))

    analyses = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(analyze, symbol, ticker): symbol
            for symbol, ticker in candidates
        }

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    analyses.append(result)
            except Exception as e:
                print("Worker error:", str(e))

    analyses.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    # News classification
    news = get_news()
    news_results = classify_news(
        news,
        list(symbols.keys())
    )

    # ---------------- NEWS ALERT ----------------

    for item in news_results:
        key = "news_" + str(item["title"])

        if key in state["alerts"]:
            continue

        state["alerts"][key] = True

        coins = (
            ", ".join(item["coins"])
            if item["coins"] else "No exact coin match"
        )

        telegram(
            f"📰 <b>KUCOIN NEWS: {item['sentiment']}</b>\n"
            f"{item['title']}\n"
            f"🪙 Related coins: {coins}\n"
            f"Source: KuCoin official announcements"
        )

    # ---------------- SIGNAL ALERTS ----------------

    for x in analyses:
        key = (
            f"{x['symbol']}_{x['signal']}_"
            f"{int(x['price'] * 100000)}"
        )

        if key in state["alerts"]:
            continue

        state["alerts"][key] = True

        telegram(
            signal_message(x, deposit_off)
        )

    # Keep state from growing forever.
    if len(state["alerts"]) > 2000:
        state["alerts"] = dict(
            list(state["alerts"].items())[-1000:]
        )

    # ---------------- DAILY REPORT ----------------

    today = datetime.now(BD_TZ).strftime("%Y-%m-%d")

    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_sent"] = False

    if not state.get("daily_sent"):
        best = analyses[:TOP_SIGNALS]

        lines = [
            "🌞 <b>TODAY'S DEPOSIT-OFF BULLISH WATCHLIST</b>",
            "━━━━━━━━━━━━━━",
            f"📅 {today}",
            f"Total all-network deposit OFF coins: {len(deposit_off)}",
            ""
        ]

        if best:
            for i, x in enumerate(best, 1):
                lines.append(
                    f"{i}. <b>{x['symbol']}</b> | "
                    f"{x['signal']}\n"
                    f"Price: {fmt_price(x['price'])} | "
                    f"24h: {x['change']:.2f}%\n"
                    f"Volume: {x['volume_ratio']:.2f}x | "
                    f"Buy: {x['buy_ratio']*100:.1f}%\n"
                    f"RSI: {x['rsi']:.1f} | "
                    f"Score: {x['score']}/100\n"
                    f"Entry: {fmt_price(x['entry_low'])} - "
                    f"{fmt_price(x['entry_high'])}\n"
                    f"SL: {fmt_price(x['stop'])} | "
                    f"T1: {fmt_price(x['target1'])}\n"
                )
        else:
            lines.append(
                "No strong bullish setup found today."
            )

        lines.append(
            "\n⚠️ Watchlist only. Signals can fail."
        )

        telegram("\n".join(lines))
        state["daily_sent"] = True

    save_state(state)
    print("Scan completed.")


if __name__ == "__main__":
    main()
