import os
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

KUCOIN_API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# =========================================================
# SETTINGS
# =========================================================

# Deposit OFF = highest priority
CHECK_DEPOSIT_OFF = True

# Existing coins only
MIN_LISTED_DAYS = 30

# Market cap:
# Ultra-micro: < $1M
# Micro:       $1M - $100M
# Mid/High:    >= $100M -> excluded
MAX_MARKET_CAP = 100_000_000

# Fresh same-day setup
MIN_FIRST_MOVE = 20.0
MIN_RETRACE = 15.0
MAX_ABOVE_LOW = 15.0

# Buying activity confirmation
VOLUME_MULTIPLIER = 1.20

# Bangladesh day
BD_TZ = ZoneInfo("Asia/Dhaka")


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

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


# =========================================================
# KUCOIN SYMBOLS
# =========================================================

def get_symbols():

    url = f"{KUCOIN_API}/api/v2/symbols"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        print("Symbol error:", e)
        return []

    now_ms = int(time.time() * 1000)

    result = []

    for x in data:

        try:
            symbol = x["symbol"]

            if x.get("quoteCurrency") != "USDT":
                continue

            if not x.get("enableTrading"):
                continue

            # New listing বাদ
            list_at = x.get("listAt")

            if list_at:
                age_days = (
                    now_ms - int(list_at)
                ) / 1000 / 86400

                if age_days < MIN_LISTED_DAYS:
                    continue

            result.append({
                "symbol": symbol,
                "base": x.get("baseCurrency", "")
            })

        except Exception:
            continue

    print("Eligible existing coins:", len(result))

    return result


# =========================================================
# KUCOIN DEPOSIT STATUS
# =========================================================

def get_deposit_status():

    url = f"{KUCOIN_API}/api/v3/currencies"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()

        data = r.json().get("data", [])

        result = {}

        for coin in data:

            currency = coin.get("currency")

            chains = coin.get("chains") or []

            if not currency:
                continue

            # True = at least one network deposit ON
            deposit_on = False

            for chain in chains:

                if chain.get("isDepositEnabled") is True:
                    deposit_on = True
                    break

            result[currency.upper()] = deposit_on

        return result

    except Exception as e:
        print("Deposit API error:", e)
        return {}


def check_deposit_off():

    status = get_deposit_status()

    if not status:
        return

    alerts = []

    for coin, deposit_on in status.items():

        if not deposit_on:

            # Deposit OFF
            alerts.append(coin)

    if not alerts:
        print("No deposit OFF detected.")
        return

    # Only coins currently in KuCoin market universe
    symbols = get_symbols()

    market_bases = {
        x["base"].upper()
        for x in symbols
    }

    for coin in alerts:

        if coin not in market_bases:
            continue

        message = (
            "🚨 KUCOIN DEPOSIT OFF\n\n"
            f"🪙 {coin}\n"
            "⛔ Deposit: OFF\n\n"
            "🔥 HIGH PRIORITY"
        )

        print(message)

        send_telegram(message)


# =========================================================
# MARKET CAP
# =========================================================

def get_market_caps():

    """
    CoinGecko public API.
    Used only to classify:
    Ultra-micro / Micro / Mid / High.
    """

    caps = {}

    for page in range(1, 5):

        try:

            url = "https://api.coingecko.com/api/v3/coins/markets"

            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false"
            }

            r = requests.get(
                url,
                params=params,
                timeout=20
            )

            if r.status_code != 200:
                print("CoinGecko:", r.status_code)
                break

            data = r.json()

            if not data:
                break

            for coin in data:

                symbol = (
                    coin.get("symbol") or ""
                ).upper()

                mc = coin.get("market_cap")

                if symbol and mc:
                    caps[symbol] = mc

            time.sleep(1)

        except Exception as e:

            print("Market cap error:", e)
            break

    print("Market cap data:", len(caps))

    return caps


# =========================================================
# 15M CANDLES
# =========================================================

def get_15m_candles(symbol):

    # Bangladesh current date
    now_bd = datetime.now(BD_TZ)

    # Start of TODAY
    start_bd = now_bd.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    start_utc = int(
        start_bd.astimezone(
            timezone.utc
        ).timestamp()
    )

    end_utc = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    url = f"{KUCOIN_API}/api/v1/market/candles"

    params = {
        "symbol": symbol,
        "type": "15min",
        "startAt": start_utc,
        "endAt": end_utc
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
                pass

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
# 1H CANDLES
# =========================================================

def get_1h_candles(symbol):

    now = int(time.time())

    start = now - 24 * 60 * 60

    url = f"{KUCOIN_API}/api/v1/market/candles"

    params = {
        "symbol": symbol,
        "type": "1hour",
        "startAt": start,
        "endAt": now
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
                    "open": float(c[1]),
                    "close": float(c[2]),
                    "high": float(c[3]),
                    "low": float(c[4])
                })

            except Exception:
                pass

        candles.sort(
            key=lambda x: x["high"]
        )

        return candles

    except Exception:
        return []


# =========================================================
# FRESH SAME-DAY RETEST
# =========================================================

def check_retest(symbol, market_cap):

    # Mid/high বাদ
    if market_cap is None:
        return None

    if market_cap >= MAX_MARKET_CAP:
        return None

    candles = get_15m_candles(symbol)

    if len(candles) < 8:
        return None

    # Last completed candle
    current = candles[-2]

    previous = candles[:-2]

    if len(previous) < 5:
        return None

    # ---------------------------------------------
    # TODAY'S LOW
    # ---------------------------------------------

    today_low = min(
        c["low"]
        for c in candles
    )

    if today_low <= 0:
        return None

    # ---------------------------------------------
    # TODAY'S HIGH / FIRST MOVE
    # ---------------------------------------------

    peak_candle = max(
        previous,
        key=lambda c: c["high"]
    )

    peak = peak_candle["high"]

    first_move = (
        (peak - today_low)
        / today_low
    ) * 100

    if first_move < MIN_FIRST_MOVE:
        return None

    # ---------------------------------------------
    # PEAK MUST HAPPEN BEFORE CURRENT CANDLE
    # ---------------------------------------------

    peak_index = previous.index(
        peak_candle
    )

    if peak_index >= len(previous) - 1:
        return None

    # ---------------------------------------------
    # CURRENT PRICE
    # ---------------------------------------------

    price = current["close"]

    above_low = (
        (price - today_low)
        / today_low
    ) * 100

    if above_low < 0:
        return None

    if above_low > MAX_ABOVE_LOW:
        return None

    # ---------------------------------------------
    # RETRACE >= 15%
    # ---------------------------------------------

    retrace = (
        (peak - price)
        / peak
    ) * 100

    if retrace < MIN_RETRACE:
        return None

    # ---------------------------------------------
    # BUYING ACTIVITY
    # ---------------------------------------------

    if current["close"] <= current["open"]:
        return None

    sample = previous[-5:]

    avg_volume = sum(
        c["volume"]
        for c in sample
    ) / len(sample)

    if avg_volume <= 0:
        return None

    volume_ratio = (
        current["volume"]
        / avg_volume
    )

    if volume_ratio < VOLUME_MULTIPLIER:
        return None

    # ---------------------------------------------
    # 1H CONFIRMATION
    # ---------------------------------------------

    h1 = get_1h_candles(symbol)

    if len(h1) < 4:
        return None

    h1_recent = h1[-4:]

    h1_high = max(
        c["high"]
        for c in h1_recent
    )

    h1_low = min(
        c["low"]
        for c in h1_recent
    )

    if h1_low <= 0:
        return None

    h1_move = (
        (h1_high - h1_low)
        / h1_low
    ) * 100

    # 1H-এও meaningful move থাকতে হবে
    if h1_move < MIN_FIRST_MOVE:
        return None

    return {
        "price": price,
        "low": today_low,
        "first_move": first_move,
        "above_low": above_low,
        "market_cap": market_cap,
        "volume_ratio": volume_ratio
    }


# =========================================================
# MAIN
# =========================================================

def main():

    print("======================================")
    print(" KUCOIN EARLY BUY / RETEST SCANNER")
    print("======================================")

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets missing.")
        return

    # -----------------------------------------------------
    # 1ST PRIORITY: DEPOSIT OFF
    # -----------------------------------------------------

    print("\n[1] Checking Deposit OFF...")

    if CHECK_DEPOSIT_OFF:
        check_deposit_off()

    # -----------------------------------------------------
    # Get coins
    # -----------------------------------------------------

    print("\n[2] Loading KuCoin coins...")

    coins = get_symbols()

    if not coins:
        print("No coins found.")
        return

    # -----------------------------------------------------
    # Market caps
    # -----------------------------------------------------

    print("\n[3] Loading market caps...")

    market_caps = get_market_caps()

    # -----------------------------------------------------
    # Retest scan
    # -----------------------------------------------------

    print("\n[4] Scanning fresh same-day retests...")

    # Micro first
    def cap_priority(item):

        mc = market_caps.get(
            item["base"].upper()
        )

        if mc is None:
            return 999

        if mc < 1_000_000:
            return 0       # Ultra-micro

        if mc < 100_000_000:
            return 1       # Micro

        return 99          # Mid/high

    coins.sort(
        key=cap_priority
    )

    alerts = 0

    for item in coins:

        symbol = item["symbol"]
        base = item["base"].upper()

        mc = market_caps.get(base)

        # Market cap unavailable হলে skip
        if mc is None:
            continue

        # Mid/high বাদ
        if mc >= MAX_MARKET_CAP:
            continue

        try:

            setup = check_retest(
                symbol,
                mc
            )

            if not setup:
                continue

            if mc < 1_000_000:
                cap_label = "Ultra-Micro"
            else:
                cap_label = "Micro"

            message = (
                "🚨 15M RETEST SETUP\n\n"
                f"🪙 {symbol}\n"
                f"💎 {cap_label}\n"
                f"📈 Today's Move: "
                f"+{setup['first_move']:.1f}%\n\n"
                f"📍 24H Low: "
                f"{setup['low']:.8g}\n"
                f"💰 Now: "
                f"{setup['price']:.8g} "
                f"(+{setup['above_low']:.1f}%)\n\n"
                "🔥 Buy activity rising"
            )

            print("\n" + message + "\n")

            if send_telegram(message):
                alerts += 1

        except Exception as e:

            print(
                f"Error {symbol}: {e}"
            )

        time.sleep(0.15)

    print("\n======================================")
    print(
        f"Scan complete. Retest alerts: {alerts}"
    )
    print("======================================")


if __name__ == "__main__":
    main()
