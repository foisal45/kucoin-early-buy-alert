import os
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://api.kucoin.com"
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
STATE_FILE = Path("state.json")
TIMEOUT = 15

# Add/adjust mappings as needed. Automatic narrative detection is heuristic,
# because KuCoin's public ticker API does not provide a reliable narrative field.
NARRATIVE_KEYWORDS = {
    "AI": ["artificial intelligence", " ai ", "ai agent", "ai"],
    "MEME": ["meme", "doge", "shib", "inu", "pepe", "bonk", "floki", "wif"],
    "GAMING": ["gaming", "game", "metaverse", "play to earn", "p2e"],
    "DEFI": ["defi", "decentralized finance", "lending", "yield", "dex"],
    "RWA": ["real world asset", " rwa ", "tokenized asset"],
    "LAYER 1": ["layer 1", "layer-1", "blockchain", "smart contract"],
    "LAYER 2": ["layer 2", "layer-2", "rollup", "scaling"],
    "DEPIN": ["depin", "decentralized physical infrastructure"],
    "AI / DEPIN": ["depin", "artificial intelligence"],
}

# Optional manual ST list: set GitHub secret/variable ST_COINS="ABC,XYZ"
MANUAL_ST = {
    x.strip().upper()
    for x in os.getenv("ST_COINS", "").split(",")
    if x.strip()
}

session = requests.Session()
session.headers.update({"User-Agent": "KuCoin-Narrative-Watchlist/1.0"})


def get_json(path, params=None):
    response = session.get(API + path, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "200000":
        raise RuntimeError(f"KuCoin API error at {path}: {payload.get('msg')}")
    return payload.get("data")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"above_100": [], "watchlist": [], "setup_alerts": {}, "daily_summary_date": ""}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_TOKEN or CHAT_ID GitHub secret.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = session.post(
        url,
        json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def get_st_coins():
    """Best-effort auto-detection from recent KuCoin announcements + manual list."""
    st = set(MANUAL_ST)
    terms = ("st tag", "special treatment", "st-tag", "will add st")
    for page in range(1, 4):
        try:
            data = get_json(
                "/api/v3/announcements",
                {"currentPage": page, "pageSize": 100, "annType": "latest-announcements", "lang": "en_US"},
            )
            for item in (data or {}).get("items", []):
                title = str(item.get("annTitle", ""))
                desc = str(item.get("annDesc", ""))
                combined = (title + " " + desc).lower()
                if any(term in combined for term in terms):
                    # Extract likely tickers shown in parentheses, e.g. Token (ABC).
                    for match in re.findall(r"\(([A-Z0-9]{2,15})\)", title + " " + desc):
                        st.add(match.upper())
        except Exception as exc:
            print(f"ST announcement scan warning: {exc}")
            break
    return st


def classify_narrative(name, symbol):
    text = f" {name or ''} {symbol or ''} ".lower()
    # More specific category first.
    for category in ("AI / DEPIN", "AI", "MEME", "GAMING", "DEFI", "RWA", "LAYER 2", "LAYER 1", "DEPIN"):
        if any(word in text for word in NARRATIVE_KEYWORDS[category]):
            return category
    return "UNKNOWN"


def fetch_market_data():
    tickers = get_json("/api/v1/market/allTickers")["ticker"]
    symbols = get_json("/api/v2/symbols")
    currencies = get_json("/api/v3/currencies")

    symbol_info = {
        s.get("symbol"): s for s in symbols
        if s.get("quoteCurrency") == "USDT" and s.get("enableTrading", True)
    }
    currency_info = {c.get("currency"): c for c in currencies if c.get("currency")}
    usdt = []
    for ticker in tickers:
        pair = ticker.get("symbol", "")
        if not pair.endswith("-USDT") or pair not in symbol_info:
            continue
        try:
            change = float(ticker.get("changeRate") or 0) * 100
            volume_usd = float(ticker.get("volValue") or 0)
            price = float(ticker.get("last") or 0)
        except (TypeError, ValueError):
            continue
        base = pair[:-5]
        info = symbol_info[pair]
        currency = currency_info.get(base, {})
        full_name = currency.get("fullName") or currency.get("name") or base
        usdt.append({
            "pair": pair, "base": base, "change": change, "volume_usd": volume_usd,
            "price": price, "name": full_name, "symbol_info": info,
        })
    usdt.sort(key=lambda x: x["change"], reverse=True)
    return usdt, currency_info


def fully_deposit_off(currency_info):
    """True only when every listed network is explicitly deposit-disabled."""
    result = set()
    for code, info in currency_info.items():
        chains = info.get("chains") or []
        if chains and all(chain.get("isDepositEnabled") is False for chain in chains):
            result.add(code.upper())
    return result


def get_candles(pair, candle_type, limit=100):
    data = get_json(
        "/api/v1/market/candles",
        {"symbol": pair, "type": candle_type},
    )
    # KuCoin returns newest-first: [time, open, close, high, low, volume, turnover].
    candles = []
    for row in (data or [])[:limit]:
        try:
            candles.append({
                "time": int(row[0]), "open": float(row[1]), "close": float(row[2]),
                "high": float(row[3]), "low": float(row[4]), "volume": float(row[5]),
            })
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(candles, key=lambda x: x["time"])


def setup_check(coin):
    """Conservative heuristic: 1H structure + green 15m candle/volume + near 24h low."""
    pair = coin["pair"]
    hourly = get_candles(pair, "1hour", 12)
    fifteen = get_candles(pair, "15min", 100)
    if len(hourly) < 7 or len(fifteen) < 9:
        return False, "Not enough candles"

    h_closes = [c["close"] for c in hourly]
    h_avg = sum(h_closes[-6:]) / 6
    h_ok = h_closes[-1] > h_avg and h_closes[-1] >= h_closes[-2]

    latest = fifteen[-1]
    prior_vols = [c["volume"] for c in fifteen[-9:-1]]
    avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
    m15_ok = latest["close"] > latest["open"] and avg_vol > 0 and latest["volume"] >= avg_vol * 1.2

    # Current price should remain within +15% of 24h low, matching the requested retest zone.
    low_24h = min(c["low"] for c in fifteen[-96:]) if len(fifteen) >= 96 else min(c["low"] for c in fifteen)
    near_low = coin["price"] > 0 and coin["price"] <= low_24h * 1.15
    if h_ok and m15_ok and near_low:
        return True, "1H trend + 15M green/volume + near-low retest"
    return False, "Waiting for confirmation"


def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Set TELEGRAM_TOKEN and CHAT_ID in GitHub Secrets.")

    state = load_state()
    st_coins = get_st_coins()
    gainers, currency_info = fetch_market_data()
    off_coins = fully_deposit_off(currency_info)

    # Do not include ST-tagged coins anywhere in alerts/watchlist.
    gainers = [c for c in gainers if c["base"] not in st_coins]
    top_gainers = gainers[:30]
    top_narratives = {}
    for coin in top_gainers:
        narrative = classify_narrative(coin["name"], coin["base"])
        if narrative != "UNKNOWN":
            top_narratives[narrative] = top_narratives.get(narrative, 0) + 1

    active_narratives = {
        category for category, count in top_narratives.items() if count >= 2
    }
    if not active_narratives and top_narratives:
        active_narratives = {max(top_narratives, key=top_narratives.get)}

    # Alert only when a symbol newly crosses +100%; reset after it falls below +95%.
    currently_above = {c["base"] for c in gainers if c["change"] >= 100}
    previous_above = set(state.get("above_100", []))
    for coin in gainers:
        if coin["change"] >= 100 and coin["base"] not in previous_above:
            narrative = classify_narrative(coin["name"], coin["base"])
            telegram(
                "🚀 KUCOIN +100% TOP GAINER\n"
                f"{coin['pair']} | 24H: +{coin['change']:.1f}%\n"
                f"Narrative: {narrative}\n"
                f"24H turnover: ${coin['volume_usd']:,.0f}\n"
                "⚠️ Momentum alert only—not a buy signal."
            )
    state["above_100"] = sorted(
        currently_above | {x for x in previous_above if x in currently_above}
    )

    # Match fully deposit-off coins against currently active top-gainer narratives.
    by_base = {c["base"]: c for c in gainers}
    watch = []
    for base in off_coins:
        if base in st_coins or base not in by_base:
            continue
        coin = by_base[base]
        narrative = classify_narrative(coin["name"], base)
        if narrative in active_narratives:
            watch.append((coin, narrative))
    watch.sort(key=lambda x: x[0]["volume_usd"], reverse=True)
    watch = watch[:20]

    new_watch = []
    prior_watch = set(state.get("watchlist", []))
    current_watch = {coin["base"] for coin, _ in watch}
    for coin, narrative in watch:
        if coin["base"] not in prior_watch:
            new_watch.append((coin, narrative))
    if new_watch:
        lines = ["🔎 DEPOSIT-OFF / SAME-NARRATIVE WATCHLIST"]
        lines.append("Active top-gainer trend: " + ", ".join(sorted(active_narratives)))
        for coin, narrative in new_watch[:12]:
            lines.append(
                f"• {coin['pair']} | {narrative} | 24H {coin['change']:+.1f}% "
                f"| turnover ${coin['volume_usd']:,.0f}"
            )
        lines.append("ST coins excluded. Watchlist ≠ buy recommendation.")
        telegram("\n".join(lines))
    state["watchlist"] = sorted(current_watch)

    # Technical confirmation alerts, limited to the highest-turnover watchlist coins.
    setup_alerts = state.get("setup_alerts", {})
    for coin, narrative in watch[:12]:
        try:
            ready, reason = setup_check(coin)
        except Exception as exc:
            print(f"Candle check failed for {coin['pair']}: {exc}")
            continue
        now = int(time.time())
        last_alert = int(setup_alerts.get(coin["base"], 0) or 0)
        if ready and now - last_alert >= 6 * 3600:
            telegram(
                "🚨 DEPOSIT-OFF RETEST SETUP\n"
                f"{coin['pair']} | Narrative: {narrative}\n"
                f"24H change: {coin['change']:+.1f}%\n"
                f"Price: {coin['price']}\n"
                f"Confirmation: {reason}\n"
                "⚠️ High risk; verify liquidity and news before trading."
            )
            setup_alerts[coin["base"]] = now
    state["setup_alerts"] = setup_alerts

    # One daily trend summary (UTC date) to avoid Telegram spam.
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("daily_summary_date") != today:
        lines = ["📊 KUCOIN DAILY TREND (UTC)", "Top gainers:"]
        for coin in top_gainers[:10]:
            narrative = classify_narrative(coin["name"], coin["base"])
            lines.append(f"• {coin['pair']} {coin['change']:+.1f}% | {narrative}")
        lines.append("Active narrative(s): " + (", ".join(sorted(active_narratives)) or "Not clear"))
        lines.append(f"Fully deposit-off coins matched to active narrative: {len(watch)}")
        lines.append("ST-tag filter: best-effort auto-detection + optional ST_COINS list.")
        telegram("\n".join(lines))
        state["daily_summary_date"] = today

    save_state(state)
    print(
        f"Done. top_gainers={len(top_gainers)}, fully_off={len(off_coins)}, "
        f"active_narratives={sorted(active_narratives)}, watchlist={len(watch)}"
    )


if __name__ == "__main__":
    main()
