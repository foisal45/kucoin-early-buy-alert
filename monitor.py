import os
import time
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


# =========================================================
# CONFIG
# =========================================================

KUCOIN_API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

BD_TZ = ZoneInfo("Asia/Dhaka")

# -------------------------
# Listing
# -------------------------
MIN_LISTED_DAYS = 30

# -------------------------
# Market cap
# -------------------------
# Ultra-micro: < $1M
# Micro:       $1M - $100M
# $100M+:      excluded
MAX_MARKET_CAP = 100_000_000

# -------------------------
# Fresh move
# -------------------------
MIN_FIRST_MOVE = 20.0

# -------------------------
# Retrace
# -------------------------
MIN_RETRACE = 15.0

# -------------------------
# Current price zone
# -------------------------
MAX_ABOVE_LOW = 15.0

# -------------------------
# Buying confirmation
# -------------------------
VOLUME_MULTIPLIER = 1.20


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram secrets missing.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        print(
            "Telegram status:",
            response.status_code
        )

        return response.status_code == 200

    except Exception as e:

        print(
            "Telegram error:",
            e
        )

        return False


# =========================================================
# KUCOIN SYMBOLS
# =========================================================

def get_symbols():

    url = f"{KUCOIN_API}/api/v2/symbols"

    try:

        response = requests.get(
            url,
            timeout=20
        )

        response.raise_for_status()

        data = response.json().get(
            "data",
            []
        )

    except Exception as e:

        print(
            "Symbol API error:",
            e
        )

        return []

    now_ms = int(
        time.time() * 1000
    )

    coins = []

    for item in data:

        try:

            symbol = item.get("symbol")
            base = item.get("baseCurrency")
            quote = item.get("quoteCurrency")

            # USDT only
            if quote != "USDT":
                continue

            # Trading enabled
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

            coins.append({
                "symbol": symbol,
                "base": base
            })

        except Exception:
            continue

    print(
        f"Existing USDT coins: {len(coins)}"
    )

    return coins


# =========================================================
# DEPOSIT STATUS
# =========================================================

def get_deposit_off_coins():

    """
    Returns coins where ALL available networks
    currently have deposit disabled.

    IMPORTANT:
    This does NOT send a Telegram message.
    It is only used as HIGH PRIORITY information.
    """

    url = (
        f"{KUCOIN_API}/api/v3/currencies"
    )

    try:

        response = requests.get(
            url,
            timeout=20
        )

        response.raise_for_status()

        data = response.json().get(
            "data",
            []
        )

    except Exception as e:

        print(
            "Deposit API error:",
            e
        )

        return set()

    deposit_off = set()

    for coin in data:

        currency = coin.get(
            "currency"
        )

        if not currency:
            continue

        chains = coin.get(
            "chains"
        ) or []

        if not chains:
            continue

        # ALL networks must be OFF
        all_off = True

        for chain in chains:

            if chain.get(
                "isDepositEnabled"
            ) is True:

                all_off = False
                break

        if all_off:

            deposit_off.add(
                currency.upper()
            )

    print(
        f"Deposit OFF coins found: "
        f"{len(deposit_off)}"
    )

    return deposit_off


# =========================================================
# MARKET CAP
# =========================================================

def get_market_caps():

    """
    CoinGecko market-cap data.

    We load the smaller market-cap pages first
    because our target is micro / ultra-micro.
    """

    caps = {}

    # CoinGecko orders by market cap descending.
    # We therefore scan several pages.
    for page in range(1, 5):

        url = (
            "https://api.coingecko.com/"
            "api/v3/coins/markets"
        )

        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "sparkline": "false"
        }

        try:

            response = requests.get(
                url,
                params=params,
                timeout=20
            )

            if response.status_code != 200:

                print(
                    "CoinGecko status:",
                    response.status_code
                )

                break

            data = response.json()

            if not data:
                break

            for coin in data:

                symbol = (
                    coin.get("symbol")
                    or ""
                ).upper()

                market_cap = coin.get(
                    "market_cap"
                )

                if symbol and market_cap:
                    caps[symbol] = market_cap

            time.sleep(1)

        except Exception as e:

            print(
                "Market cap error:",
                e
            )

            break

    print(
        f"Market cap records: {len(caps)}"
    )

    return caps


# =========================================================
# TODAY'S START TIME
# =========================================================

def today_start_utc():

    now_bd = datetime.now(
        BD_TZ
    )

    start_bd = now_bd.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    return int(
        start_bd
        .astimezone(timezone.utc)
        .timestamp()
    )


# =========================================================
# 15M CANDLES
# =========================================================

def get_15m_candles(symbol):

    start = today_start_utc()

    end = int(
        datetime.now(
            timezone.utc
        ).timestamp()
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

        response = requests.get(
            url,
            params=params,
            timeout=15
        )

        if response.status_code != 200:
            return []

        raw = response.json().get(
            "data",
            []
        )

        candles = []

        for candle in raw:

            try:

                candles.append({
                    "time": int(candle[0]),
                    "open": float(candle[1]),
                    "close": float(candle[2]),
                    "high": float(candle[3]),
                    "low": float(candle[4]),
                    "volume": float(candle[5])
                })

            except Exception:
                continue

        candles.sort(
            key=lambda x: x["time"]
        )

        return candles

    except Exception as e:

        print(
            f"15M error {symbol}:",
            e
        )

        return []


# =========================================================
# 1H CANDLES
# =========================================================

def get_1h_candles(symbol):

    start = today_start_utc()

    end = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    url = (
        f"{KUCOIN_API}/api/v1/"
        f"market/candles"
    )

    params = {
        "symbol": symbol,
        "type": "1hour",
        "startAt": start,
        "endAt": end
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=15
        )

        if response.status_code != 200:
            return []

        raw = response.json().get(
            "data",
            []
        )

        candles = []

        for candle in raw:

            try:

                candles.append({
                    "time": int(candle[0]),
                    "open": float(candle[1]),
                    "close": float(candle[2]),
                    "high": float(candle[3]),
                    "low": float(candle[4])
                })

            except Exception:
                continue

        candles.sort(
            key=lambda x: x["time"]
        )

        return candles

    except Exception as e:

        print(
            f"1H error {symbol}:",
            e
        )

        return []


# =========================================================
# RETEST CHECK
# =========================================================

def check_retest(
    symbol,
    market_cap
):

    # -----------------------------------------------------
    # Market cap filter
    # -----------------------------------------------------

    if market_cap is None:
        return None

    # Mid / high cap excluded
    if market_cap >= MAX_MARKET_CAP:
        return None

    # -----------------------------------------------------
    # 15M
    # -----------------------------------------------------

    candles = get_15m_candles(
        symbol
    )

    if len(candles) < 8:
        return None

    # Last COMPLETED candle
    current = candles[-2]

    previous = candles[:-2]

    if len(previous) < 5:
        return None

    # -----------------------------------------------------
    # TODAY'S LOW
    # -----------------------------------------------------

    today_low = min(
        c["low"]
        for c in candles
    )

    if today_low <= 0:
        return None

    # -----------------------------------------------------
    # TODAY'S HIGH / FIRST MOVE
    # -----------------------------------------------------

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

    # Peak must happen before current candle
    peak_index = previous.index(
        peak_candle
    )

    if peak_index >= len(previous) - 1:
        return None

    # -----------------------------------------------------
    # CURRENT PRICE VS TODAY'S LOW
    # -----------------------------------------------------

    price = current["close"]

    above_low = (
        (price - today_low)
        / today_low
    ) * 100

    if above_low < 0:
        return None

    if above_low > MAX_ABOVE_LOW:
        return None

    # -----------------------------------------------------
    # RETRACE
    # -----------------------------------------------------

    retrace = (
        (peak - price)
        / peak
    ) * 100

    if retrace < MIN_RETRACE:
        return None

    # -----------------------------------------------------
    # BUYING ACTIVITY
    #
    # Current completed 15M candle:
    # Green + volume higher than recent average
    # -----------------------------------------------------

    if current["close"] <= current["open"]:
        return None

    recent = previous[-5:]

    avg_volume = sum(
        c["volume"]
        for c in recent
    ) / len(recent)

    if avg_volume <= 0:
        return None

    volume_ratio = (
        current["volume"]
        / avg_volume
    )

    if volume_ratio < VOLUME_MULTIPLIER:
        return None

    # -----------------------------------------------------
    # 1H CONFIRMATION
    # -----------------------------------------------------

    h1 = get_1h_candles(
        symbol
    )

    if len(h1) < 2:
        return None

    h1_low = min(
        c["low"]
        for c in h1
    )

    h1_high = max(
        c["high"]
        for c in h1
    )

    if h1_low <= 0:
        return None

    h1_move = (
        (h1_high - h1_low)
        / h1_low
    ) * 100

    if h1_move < MIN_FIRST_MOVE:
        return None

    return {
        "price": price,
        "low": today_low,
        "first_move": first_move,
        "above_low": above_low,
        "volume_ratio": volume_ratio
    }


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "======================================"
    )
    print(
        " KUCOIN FRESH RETEST SCANNER"
    )
    print(
        "======================================"
    )

    if not TELEGRAM_TOKEN or not CHAT_ID:

        print(
            "ERROR: Telegram secrets missing."
        )

        return

    # -----------------------------------------------------
    # 1. EXISTING COINS
    # -----------------------------------------------------

    symbols = get_symbols()

    if not symbols:
        print("No eligible coins.")
        return

    # -----------------------------------------------------
    # 2. DEPOSIT OFF
    #
    # NO ALERT HERE.
    # Only used as priority flag.
    # -----------------------------------------------------

    deposit_off = get_deposit_off_coins()

    # -----------------------------------------------------
    # 3. MARKET CAPS
    # -----------------------------------------------------

    market_caps = get_market_caps()

    # -----------------------------------------------------
    # 4. MICRO FIRST
    # -----------------------------------------------------

    def scan_priority(item):

        base = item["base"].upper()

        mc = market_caps.get(
            base
        )

        if mc is None:
            return 999

        if mc < 1_000_000:
            return 0

        if mc < 100_000_000:
            return 1

        return 999

    symbols.sort(
        key=scan_priority
    )

    # -----------------------------------------------------
    # 5. SCAN
    # -----------------------------------------------------

    alerts = 0

    for item in symbols:

        symbol = item["symbol"]
        base = item["base"].upper()

        market_cap = market_caps.get(
            base
        )

        # No market-cap data
        if market_cap is None:
            continue

        # Mid / high cap
        if market_cap >= MAX_MARKET_CAP:
            continue

        try:

            setup = check_retest(
                symbol,
                market_cap
            )

            if not setup:
                continue

            # -------------------------------------------------
            # CAP LABEL
            # -------------------------------------------------

            if market_cap < 1_000_000:
                cap_label = "Ultra-Micro"
            else:
                cap_label = "Micro"

            # -------------------------------------------------
            # DEPOSIT OFF = PRIORITY
            # -------------------------------------------------

            if base in deposit_off:

                title = (
                    "🚨🔥 HIGH PRIORITY RETEST"
                )

                deposit_line = (
                    "⛔ Deposit OFF"
                )

            else:

                title = (
                    "🚨 15M RETEST SETUP"
                )

                deposit_line = ""

            # -------------------------------------------------
            # MESSAGE
            # -------------------------------------------------

            message = (
                f"{title}\n\n"
                f"🪙 {symbol}\n"
                f"💎 {cap_label}\n"
            )

            if deposit_line:
                message += (
                    f"{deposit_line}\n"
                )

            message += (
                f"📈 Today's Move: "
                f"+{setup['first_move']:.1f}%\n\n"
                f"📍 24H Low: "
                f"{setup['low']:.8g}\n"
                f"💰 Now: "
                f"{setup['price']:.8g} "
                f"(+{setup['above_low']:.1f}%)\n\n"
                "🔥 Buy activity rising"
            )

            print(
                "\n" +
                message +
                "\n"
            )

            if send_telegram(
                message
            ):
                alerts += 1

        except Exception as e:

            print(
                f"Scan error {symbol}:",
                e
            )

        # API pressure কমানো
        time.sleep(0.15)

    print(
        "======================================"
    )

    print(
        f"Scan complete. "
        f"Alerts: {alerts}"
    )

    print(
        "======================================"
    )


# =========================================================
# RUN ONCE
# =========================================================

if __name__ == "__main__":
    main()
