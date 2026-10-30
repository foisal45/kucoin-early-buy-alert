
import os
import re
import json
import html
import time
import logging
from datetime import datetime, timezone

import requests

# ================= CONFIG =================

BASE = "https://api.kucoin.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

ST_COINS = {
    x.strip().upper()
    for x in os.getenv("ST_COINS", "").split(",")
    if x.strip()
}

STATE_FILE = "state.json"

MIN_24H_TURNOVER = 100000
MIN_15M_TURNOVER = 10000
MIN_VOLUME_RATIO = 2.0
MAX_24H_CHANGE = 12.0
MAX_1H_CHANGE = 3.5
MAX_NEWS_AGE_HOURS = 72

MAX_MARKETS_TO_CHECK = 40
MAX_SIGNALS_PER_RUN = 5

POSITIVE_WORDS = [
    "partnership", "partnered", "integration", "integrates",
    "launches", "launch", "mainnet", "upgrade", "ecosystem",
    "funding", "investment", "funded", "burn", "buyback",
    "adoption", "support", "collaboration", "expansion",
    "staking", "gempool", "campaign"
]

NEGATIVE_WORDS = [
    "delist", "delisting", "suspend", "suspension",
    "maintenance", "will close", "close deposit",
    "close withdrawal", "risk warning", "warning",
    "hack", "exploit", "incident", "investigation"
]

LISTING_WORDS = [
    "gets listed", "will be listed", "new listing",
    "listing on kucoin", "world premiere"
]

session = requests.Session()
session.headers.update({"User-Agent": "KuCoin-Early-Buy-Scanner/1.0"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


# ================= BASIC HELPERS =================

def api_get(path, params=None):
    response = session.get(
        BASE + path,
        params=params,
        timeout=15
    )
    response.raise_for_status()

    result = response.json()

    if result.get("code") != "200000":
        raise RuntimeError(f"KuCoin API error: {result}")

    return result.get("data", {})


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    # Keep the state file small
    state["sent"] = state.get("sent", [])[-500:]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logging.error("Telegram secrets missing")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        response = session.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=15
        )
        response.raise_for_status()

        result = response.json()
        if not result.get("ok"):
            logging.error("Telegram error: %s", result)
            return False

        return True

    except requests.RequestException as exc:
        logging.error("Telegram send failed: %s", exc)
        return False


# ================= KUCOIN MARKET DATA =================

def get_symbols():
    data = api_get("/api/v2/symbols")
    return data if isinstance(data, list) else []


def get_tickers():
    data = api_get("/api/v1/market/allTickers")
    return data.get("ticker", [])


def get_announcements():
    data = api_get(
        "/api/v3/announcements",
        {
            "currentPage": 1,
            "pageSize": 100,
            "annType": "latest-announcements",
            "lang": "en_US"
        }
    )
    return data.get("items", [])


def get_candles(symbol):
    data = api_get(
        "/api/v1/market/candles",
        {"symbol": symbol, "type": "15min"}
    )

    if not isinstance(data, list):
        return []

    candles = []

    for row in data:
        if len(row) < 7:
            continue

        candles.append({
            "time": int(row[0]),
            "open": number(row[1]),
            "close": number(row[2]),
            "high": number(row[3]),
            "low": number(row[4]),
            "volume": number(row[5]),
            "turnover": number(row[6])
        })

    candles.sort(key=lambda x: x["time"])
    return candles


def get_deposit_status(currency):
    try:
        data = api_get(f"/api/v3/currencies/{currency}")
        chains = data.get("chains", [])

        if not chains:
            return "UNKNOWN"

        # Deposit OFF only if every known network explicitly says OFF
        statuses = [
            chain.get("isDepositEnabled")
            for chain in chains
        ]

        if statuses and all(status is False for status in statuses):
            return "OFF"

        if any(status is True for status in statuses):
            return "ON"

        return "UNKNOWN"

    except Exception as exc:
        logging.warning(
            "Could not check deposit status for %s: %s",
            currency, exc
        )
        return "UNKNOWN"


# ================= NEWS FILTER =================

def announcement_text(item):
    title = item.get("annTitle", "")
    description = item.get("annDesc", "")
    return f"{title} {description}".lower()


def is_new_listing(item):
    text = announcement_text(item)
    types = item.get("annType", [])

    if isinstance(types, str):
        types = [types]

    if "new-listings" in types:
        return True

    return any(word in text for word in LISTING_WORDS)


def coin_mentioned(item, symbol):
    title = item.get("annTitle", "")
    description = item.get("annDesc", "")
    text = f"{title} {description}".upper()

    # Prefer exact ticker references to reduce false matches
    patterns = [
        rf"\({re.escape(symbol)}\)",
        rf"\${re.escape(symbol)}\b",
        rf"\b{re.escape(symbol)}/USDT\b"
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def find_positive_news(symbol, announcements):
    now_ms = int(time.time() * 1000)

    for item in announcements:
        text = announcement_text(item)

        if is_new_listing(item):
            continue

        if any(word in text for word in NEGATIVE_WORDS):
            continue

        if not any(word in text for word in POSITIVE_WORDS):
            continue

        if not coin_mentioned(item, symbol):
            continue

        published = number(item.get("cTime"))

        if published:
            age_hours = (now_ms - published) / 3600000
            if age_hours < 0 or age_hours > MAX_NEWS_AGE_HOURS:
                continue
        else:
            continue

        title = item.get("annTitle", "KuCoin announcement")
        url = item.get("annUrl", "")

        if not url.startswith("https://www.kucoin.com/"):
            continue

        return {
            "title": title,
            "url": url,
            "age_hours": age_hours,
            "id": str(item.get("annId", ""))
        }

    return None


# ================= TECHNICAL ANALYSIS =================

def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    result = [sum(values[:period]) / period]

    for value in values[period:]:
        result.append(
            (value - result[-1]) * multiplier + result[-1]
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

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd_histogram(values):
    fast = ema(values, 12)
    slow = ema(values, 26)

    if not fast or not slow:
        return None, None

    # Align EMA arrays by their original candle positions
    offset = 26 - 12
    macd_line = [
        fast[i + offset] - slow[i]
        for i in range(len(slow))
    ]

    signal = ema(macd_line, 9)

    if len(signal) < 2:
        return None, None

    hist = [
        macd_line[i + (len(macd_line) - len(signal))]
        - signal[i]
        for i in range(len(signal))
    ]

    return hist[-1], hist[-2]


def analyse_market(symbol):
    candles = get_candles(symbol)

    # The newest candle may still be forming; use completed candles
    if len(candles) < 60:
        return None

    completed = candles[:-1]

    if len(completed) < 55:
        return None

    closes = [c["close"] for c in completed]
    highs = [c["high"] for c in completed]
    lows = [c["low"] for c in completed]

    last = completed[-1]
    previous = completed[-2]

    ema20_series = ema(closes, 20)
    ema50_series = ema(closes, 50)

    if not ema20_series or not ema50_series:
        return None

    ema20 = ema20_series[-1]
    ema50 = ema50_series[-1]
    current_rsi = rsi(closes)

    macd_now, macd_previous = macd_histogram(closes)

    if current_rsi is None or macd_now is None:
        return None

    # Last completed 15m candle compared with the previous 20 candles
    previous_volumes = [
        c["turnover"] for c in completed[-21:-1]
    ]

    avg_turnover = (
        sum(previous_volumes) / len(previous_volumes)
        if previous_volumes else 0
    )

    volume_ratio = (
        last["turnover"] / avg_turnover
        if avg_turnover > 0 else 0
    )

    # Price movement over the last four completed 15m candles
    old_price = completed[-5]["close"]
    change_1h = (
        ((last["close"] - old_price) / old_price) * 100
        if old_price else 0
    )

    # Recent support and resistance
    support = min(lows[-20:])
    resistance = max(highs[-20:])

    # Simple ATR estimate
    true_ranges = []

    for i in range(1, len(completed)):
        candle = completed[i]
        previous_close = completed[i - 1]["close"]

        true_ranges.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close)
        ))

    atr = (
        sum(true_ranges[-14:]) / 14
        if len(true_ranges) >= 14 else 0
    )

    technical_checks = {
        "volume": volume_ratio >= MIN_VOLUME_RATIO,
        "price_not_pumped": change_1h <= MAX_1H_CHANGE,
        "ema": last["close"] > ema20 and ema20 > ema50,
        "rsi": 50 <= current_rsi <= 68,
        "macd": macd_now > 0 and macd_now > macd_previous,
        "turnover": last["turnover"] >= MIN_15M_TURNOVER
    }

    passed = sum(technical_checks.values())

    if passed < 6:
        return None

    if support <= 0 or atr <= 0:
        return None

    # Entry near current market price; risk levels use ATR/support
    entry_low = last["close"] * 0.997
    entry_high = last["close"] * 1.003

    stop = min(
        support - (0.25 * atr),
        entry_low - (1.5 * atr)
    )

    risk = entry_high - stop

    if risk <= 0:
        return None

    target1 = entry_high + (1.5 * risk)
    target2 = entry_high + (2.5 * risk)

    return {
        "price": last["close"],
        "volume_ratio": volume_ratio,
        "change_1h": change_1h,
        "rsi": current_rsi,
        "ema20": ema20,
        "ema50": ema50,
        "macd": macd_now,
        "support": support,
        "resistance": resistance,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "target1": target1,
        "target2": target2,
        "checks": technical_checks
    }


# ================= SCANNER =================

def build_market_list(symbols, tickers):
    active = {
        item["symbol"]: item
        for item in symbols
        if item.get("quoteCurrency") == "USDT"
        and item.get("enableTrading") is True
        and item.get("symbol")
    }

    markets = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        if symbol not in active:
            continue

        base = active[symbol].get("baseCurrency", "")

        if not base or base in ST_COINS:
            continue

        change = number(ticker.get("changeRate")) * 100
        turnover = number(ticker.get("volValue"))

        if change > MAX_24H_CHANGE or change < -8:
            continue

        if turnover < MIN_24H_TURNOVER:
            continue

        markets.append({
            "symbol": symbol,
            "base": base,
            "price": number(ticker.get("last")),
            "change": change,
            "turnover": turnover
        })

    markets.sort(
        key=lambda x: x["turnover"],
        reverse=True
    )

    return markets[:MAX_MARKETS_TO_CHECK]


def format_price(value):
    if value >= 100:
        return f"{value:.4f}"
    if value >= 1:
        return f"{value:.6f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def make_alert(market, news, analysis, deposit):
    symbol = market["symbol"]
    base = market["base"]

    deposit_text = {
        "OFF": "🔴 OFF — priority",
        "ON": "🟢 ON",
        "UNKNOWN": "⚪ Unknown"
    }.get(deposit, "⚪ Unknown")

    checks = analysis["checks"]

    passed_names = [
        name.upper()
        for name, passed in checks.items()
        if passed
    ]

    message = (
        "🚨 <b>KUCOIN EARLY BUY SETUP</b>\n\n"
        f"🪙 <b>Coin:</b> {html.escape(symbol)}\n"
        f"💰 <b>Price:</b> {format_price(analysis['price'])}\n"
        f"📊 <b>24H Change:</b> {market['change']:.2f}%\n"
        f"📈 <b>15m Volume:</b> {analysis['volume_ratio']:.2f}x average\n"
        f"⏱ <b>1H Price Change:</b> {analysis['change_1h']:.2f}%\n"
        f"📥 <b>Deposit:</b> {deposit_text}\n\n"
        "📰 <b>Official KuCoin News</b>\n"
        f"{html.escape(news['title'])}\n"
        f"<a href=\"{html.escape(news['url'], quote=True)}\">Open news source</a>\n"
        f"News age: {news['age_hours']:.1f} hours\n\n"
        "📉 <b>Technical Confirmation</b>\n"
        f"RSI: {analysis['rsi']:.1f}\n"
        f"EMA20: {format_price(analysis['ema20'])}\n"
        f"EMA50: {format_price(analysis['ema50'])}\n"
        f"MACD histogram: {analysis['macd']:.8f}\n"
        f"Support: {format_price(analysis['support'])}\n"
        f"Resistance: {format_price(analysis['resistance'])}\n"
        f"Passed: {', '.join(passed_names)}\n\n"
        "🎯 <b>Trade Plan (estimated)</b>\n"
        f"Entry: {format_price(analysis['entry_low'])}"
        f" - {format_price(analysis['entry_high'])}\n"
        f"Target 1: {format_price(analysis['target1'])}\n"
        f"Target 2: {format_price(analysis['target2'])}\n"
        f"Stop Loss: {format_price(analysis['stop'])}\n\n"
        "⚠️ Signal is not a guarantee. Verify liquidity, spread and news "
        "before trading. Entry/targets are calculated estimates, not "
        "financial advice."
    )

    return message


def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN or CHAT_ID is missing")

    logging.info("Starting KuCoin early-buy scanner")

    state = load_state()
    sent = set(state.get("sent", []))

    symbols = get_symbols()
    tickers = get_tickers()
    announcements = get_announcements()

    markets = build_market_list(symbols, tickers)

    logging.info("Markets selected: %d", len(markets))

    sent_this_run = 0

    for market in markets:
        if sent_this_run >= MAX_SIGNALS_PER_RUN:
            break

        symbol = market["symbol"]
        base = market["base"]

        try:
            news = find_positive_news(base, announcements)

            if not news:
                continue

            analysis = analyse_market(symbol)

            if not analysis:
                continue

            alert_id = f"{symbol}:{news['id']}"

            if alert_id in sent:
                continue

            deposit = get_deposit_status(base)

            message = make_alert(
                market,
                news,
                analysis,
                deposit
            )

            if send_telegram(message):
                sent.add(alert_id)
                sent_this_run += 1

                logging.info("Alert sent: %s", symbol)

            time.sleep(0.25)

        except Exception as exc:
            logging.exception(
                "Error scanning %s: %s",
                symbol, exc
            )

    state["sent"] = list(sent)[-500:]
    state["last_scan_utc"] = datetime.now(
        timezone.utc
    ).isoformat()

    save_state(state)

    logging.info(
        "Scan complete. Alerts sent: %d",
        sent_this_run
    )


if __name__ == "__main__":
    main()
