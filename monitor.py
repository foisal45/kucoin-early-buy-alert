import os
import time
import json
import threading
from collections import defaultdict, deque

import requests
import websocket


# =========================================================
# CONFIG
# =========================================================

KUCOIN_API = "https://api.kucoin.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ---------------------------------------------------------
# EARLY BUY RULES
# ---------------------------------------------------------

# Price should not already be heavily pumped
MAX_ABOVE_24H_LOW = 20.0

# First meaningful move from 24H low
MIN_FIRST_MOVE = 15.0

# Pullback from first high
MIN_RETRACE = 8.0

# Individual aggressive buy size
MIN_BUY_USD = 250.0

# Short-window repeated buying
WINDOW_SECONDS = 120

# Minimum number of meaningful buys
MIN_BUY_COUNT = 3

# Buy USD must be this many times sell USD
BUY_SELL_RATIO = 1.50

# Current price should be above recent local low
MIN_BOUNCE = 1.0

# Don't alert same coin repeatedly
ALERT_COOLDOWN = 6 * 60 * 60

# New coins excluded
MIN_LISTED_DAYS = 30

# Keep trade history
MAX_TRADES = 500


# =========================================================
# GLOBAL STATE
# =========================================================

trade_data = defaultdict(
    lambda: deque(maxlen=MAX_TRADES)
)

last_alert = {}

lock = threading.Lock()


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

    try:

        r = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=15
        )

        print(
            "Telegram:",
            r.status_code
        )

        if r.status_code != 200:
            print(r.text)
            return False

        return True

    except Exception as e:

        print(
            "Telegram error:",
            e
        )

        return False


# =========================================================
# GET SYMBOLS
# =========================================================

def get_symbols():

    url = (
        f"{KUCOIN_API}/api/v2/symbols"
    )

    try:

        r = requests.get(
            url,
            timeout=20
        )

        r.raise_for_status()

        data = r.json().get(
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

    symbols = []

    for item in data:

        try:

            symbol = item.get(
                "symbol"
            )

            # USDT only
            if item.get(
                "quoteCurrency"
            ) != "USDT":

                continue

            # Trading enabled
            if not item.get(
                "enableTrading"
            ):

                continue

            # Exclude new listings
            list_at = item.get(
                "listAt"
            )

            if list_at:

                age_days = (
                    now_ms -
                    int(list_at)
                ) / 1000 / 86400

                if age_days < MIN_LISTED_DAYS:
                    continue

            symbols.append(
                symbol
            )

        except Exception:

            continue

    print(
        f"USDT symbols: {len(symbols)}"
    )

    return symbols


# =========================================================
# 24H TICKERS
# =========================================================

def get_24h_data():

    url = (
        f"{KUCOIN_API}/api/v1/"
        f"market/allTickers"
    )

    try:

        r = requests.get(
            url,
            timeout=20
        )

        r.raise_for_status()

        data = r.json().get(
            "data",
            {}
        )

        ticker_list = data.get(
            "ticker",
            []
        )

        result = {}

        for t in ticker_list:

            symbol = t.get(
                "symbol"
            )

            if not symbol:
                continue

            if not symbol.endswith(
                "-USDT"
            ):
                continue

            try:

                result[symbol] = {
                    "price": float(
                        t["last"]
                    ),
                    "low": float(
                        t["low"]
                    ),
                    "high": float(
                        t["high"]
                    ),
                    "change": float(
                        t["changeRate"]
                    ) * 100,
                    "volume": float(
                        t["volValue"]
                    )
                }

            except Exception:

                continue

        return result

    except Exception as e:

        print(
            "Ticker error:",
            e
        )

        return {}


# =========================================================
# GET 15M CANDLES
# =========================================================

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
# MARKET STRUCTURE
# =========================================================

def get_setup(symbol):

    candles = get_candles(
        symbol
    )

    if len(candles) < 30:
        return None

    # Ignore incomplete candle
    candles = candles[:-1]

    if len(candles) < 25:
        return None

    low_24h = min(
        c["low"]
        for c in candles
    )

    peak_candle = max(
        candles[:-2],
        key=lambda c: c["high"]
    )

    peak = peak_candle[
        "high"
    ]

    current = candles[-1]

    price = current[
        "close"
    ]

    if low_24h <= 0:
        return None

    # -----------------------------------------------------
    # First move
    # -----------------------------------------------------

    first_move = (
        (peak - low_24h)
        / low_24h
    ) * 100

    if first_move < MIN_FIRST_MOVE:
        return None

    # -----------------------------------------------------
    # Current distance from 24H low
    # -----------------------------------------------------

    above_low = (
        (price - low_24h)
        / low_24h
    ) * 100

    if above_low < 0:
        return None

    if above_low > MAX_ABOVE_24H_LOW:
        return None

    # -----------------------------------------------------
    # Retracement
    # -----------------------------------------------------

    retrace = (
        (peak - price)
        / peak
    ) * 100

    if retrace < MIN_RETRACE:
        return None

    return {
        "price": price,
        "low": low_24h,
        "peak": peak,
        "first_move": first_move,
        "above_low": above_low,
        "retrace": retrace
    }


# =========================================================
# BUY FOOTPRINT
# =========================================================

def analyze_buying(
    symbol
):

    now = time.time()

    with lock:

        trades = list(
            trade_data[symbol]
        )

    # Last 120 seconds
    recent = [
        t
        for t in trades
        if now - t["time"]
        <= WINDOW_SECONDS
    ]

    if len(recent) < MIN_BUY_COUNT:
        return None

    buys = [
        t
        for t in recent
        if t["side"] == "buy"
    ]

    sells = [
        t
        for t in recent
        if t["side"] == "sell"
    ]

    if len(buys) < MIN_BUY_COUNT:
        return None

    buy_usd = sum(
        t["usd"]
        for t in buys
    )

    sell_usd = sum(
        t["usd"]
        for t in sells
    )

    # Need actual buying pressure
    if sell_usd > 0:

        ratio = (
            buy_usd /
            sell_usd
        )

    else:

        ratio = 999.0

    if ratio < BUY_SELL_RATIO:
        return None

    # At least one meaningful buy
    large_buys = [
        t
        for t in buys
        if t["usd"] >= MIN_BUY_USD
    ]

    if not large_buys:
        return None

    return {
        "buy_count": len(buys),
        "large_buys": len(
            large_buys
        ),
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        "ratio": ratio
    }


# =========================================================
# TRADE MESSAGE
# =========================================================

def handle_trade(
    symbol,
    side,
    price,
    size
):

    try:

        price = float(price)
        size = float(size)

        usd = price * size

    except Exception:

        return

    now = time.time()

    with lock:

        trade_data[
            symbol
        ].append({
            "time": now,
            "side": side.lower(),
            "price": price,
            "size": size,
            "usd": usd
        })


# =========================================================
# SIGNAL CHECK
# =========================================================

def check_signal(
    symbol,
    market_data
):

    # 24H market data
    info = market_data.get(
        symbol
    )

    if not info:
        return

    price = info[
        "price"
    ]

    low = info[
        "low"
    ]

    if low <= 0:
        return

    above_low = (
        (price - low)
        / low
    ) * 100

    # Price must still be early
    if above_low > MAX_ABOVE_24H_LOW:
        return

    # Market structure
    setup = get_setup(
        symbol
    )

    if not setup:
        return

    # Real-time buying footprint
    buying = analyze_buying(
        symbol
    )

    if not buying:
        return

    # Cooldown
    now = time.time()

    if (
        symbol in last_alert
        and
        now - last_alert[symbol]
        < ALERT_COOLDOWN
    ):

        return

    # -----------------------------------------------------
    # ALERT
    # -----------------------------------------------------

    message = (
        "🚨 EARLY BUY SETUP\n\n"
        f"🪙 {symbol}\n"
        f"💰 Now: {price:.8g} "
        f"(+{above_low:.1f}% from 24H low)\n"
        f"📈 First Move: "
        f"+{setup['first_move']:.1f}%\n"
        f"🔄 Retrace: "
        f"-{setup['retrace']:.1f}%\n"
        f"🔥 Buys: "
        f"{buying['buy_count']} "
        f"({buying['large_buys']} large)\n"
        f"💵 Buy/Sell: "
        f"{buying['ratio']:.1f}x"
    )

    print(
        "\n" +
        message +
        "\n"
    )

    if send_telegram(
        message
    ):

        last_alert[
            symbol
        ] = now


# =========================================================
# WEBSOCKET
# =========================================================

def get_ws_token():

    url = (
        f"{KUCOIN_API}/api/v1/"
        f"bullet-public"
    )

    try:

        r = requests.post(
            url,
            timeout=20
        )

        r.raise_for_status()

        data = r.json()[
            "data"
        ]

        token = data[
            "token"
        ]

        server = data[
            "instanceServers"
        ][0]

        endpoint = server[
            "endpoint"
        ]

        ping_interval = (
            server.get(
                "pingInterval",
                18000
            )
            / 1000
        )

        return (
            endpoint,
            token,
            ping_interval
        )

    except Exception as e:

        print(
            "WebSocket token error:",
            e
        )

        return (
            None,
            None,
            18
        )


# =========================================================
# WEBSOCKET RUN
# =========================================================

def websocket_worker(
    symbols,
    market_data
):

    while True:

        try:

            endpoint, token, ping_interval = (
                get_ws_token()
            )

            if not endpoint:
                time.sleep(10)
                continue

            connect_url = (
                endpoint
                +
                "?token="
                +
                token
                +
                "&[connectId="
                +
                str(
                    int(
                        time.time()
                        * 1000
                    )
                )
                +
                "]"
            )

            def on_open(ws):

                print(
                    "WebSocket connected."
                )

                # KuCoin supports up to
                # 100 symbols/topic.
                chunk_size = 100

                for i in range(
                    0,
                    len(symbols),
                    chunk_size
                ):

                    chunk = symbols[
                        i:i + chunk_size
                    ]

                    topic = (
                        "/market/match:"
                        +
                        ",".join(
                            chunk
                        )
                    )

                    msg = {
                        "id": str(
                            int(
                                time.time()
                                * 1000
                            )
                        ),
                        "type": "subscribe",
                        "topic": topic,
                        "response": True
                    }

                    ws.send(
                        json.dumps(
                            msg
                        )
                    )

                print(
                    "Subscriptions sent:",
                    len(symbols)
                )

            def on_message(
                ws,
                message
            ):

                try:

                    data = json.loads(
                        message
                    )

                    if data.get(
                        "type"
                    ) != "message":

                        return

                    payload = data.get(
                        "data",
                        {}
                    )

                    symbol = payload.get(
                        "symbol"
                    )

                    if not symbol:
                        return

                    side = payload.get(
                        "side"
                    )

                    price = payload.get(
                        "price"
                    )

                    size = payload.get(
                        "size"
                    )

                    if not side:
                        return

                    handle_trade(
                        symbol,
                        side,
                        price,
                        size
                    )

                    # Only evaluate after
                    # meaningful trade
                    usd = (
                        float(price)
                        *
                        float(size)
                    )

                    if usd >= MIN_BUY_USD:

                        check_signal(
                            symbol,
                            market_data
                        )

                except Exception as e:

                    print(
                        "Message error:",
                        e
                    )

            def on_error(
                ws,
                error
            ):

                print(
                    "WebSocket error:",
                    error
                )

            def on_close(
                ws,
                code,
                msg
            ):

                print(
                    "WebSocket closed:",
                    code,
                    msg
                )

            ws = websocket.WebSocketApp(
                connect_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            ws.run_forever(
                ping_interval=15,
                ping_timeout=10
            )

        except Exception as e:

            print(
                "WebSocket reconnect error:",
                e
            )

        print(
            "Reconnecting in 10 seconds..."
        )

        time.sleep(10)


# =========================================================
# MARKET DATA REFRESH
# =========================================================

def market_data_worker(
    market_data
):

    while True:

        try:

            data = get_24h_data()

            if data:

                market_data.clear()
                market_data.update(
                    data
                )

                print(
                    "24H market data updated:",
                    len(data)
                )

        except Exception as e:

            print(
                "Market refresh error:",
                e
            )

        # Refresh every 60 seconds
        time.sleep(60)


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "======================================"
    )

    print(
        " KUCOIN EARLY BUY FOOTPRINT SCANNER"
    )

    print(
        "======================================"
    )

    if (
        not TELEGRAM_TOKEN
        or not CHAT_ID
    ):

        print(
            "Telegram secrets missing."
        )

        return

    symbols = get_symbols()

    if not symbols:

        print(
            "No symbols."
        )

        return

    market_data = {}

    # Initial market data
    market_data.update(
        get_24h_data()
    )

    # Start market refresh
    threading.Thread(
        target=market_data_worker,
        args=(market_data,),
        daemon=True
    ).start()

    # Start WebSocket
    websocket_worker(
        symbols,
        market_data
    )


# =========================================================

if __name__ == "__main__":

    main()
