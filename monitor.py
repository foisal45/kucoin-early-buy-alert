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



def orderbook_check(pair):
    """Approximate near-book sell pressure; walls can be spoofed and are not proof of bots."""
    data = get_json("/api/v3/market/orderbook/level2_20", {"symbol": pair}) or {}
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        return None, "order book unavailable"

    # Compare quote notional (price * size) in the first 20 levels.
    bid_value = sum(float(x[0]) * float(x[1]) for x in bids if len(x) >= 2)
    ask_value = sum(float(x[0]) * float(x[1]) for x in asks if len(x) >= 2)
    total = bid_value + ask_value
    if total <= 0:
        return None, "order book empty"
    ask_ratio = ask_value / max(bid_value, 1e-12)
    bid_share = bid_value / total * 100
    if ask_ratio >= 1.8:
        return -2, f"sell pressure high (asks/bids {ask_ratio:.2f}x)"
    if ask_ratio >= 1.3:
        return -1, f"sell pressure elevated (asks/bids {ask_ratio:.2f}x)"
    if bid_share >= 58:
        return 2, f"buy-side depth stronger ({bid_share:.0f}%)"
    if bid_share >= 52:
        return 1, f"buy-side depth slightly stronger ({bid_share:.0f}%)"
    return 0, f"order book balanced (bid depth {bid_share:.0f}%)"

def setup_check(coin):
    """Score an early entry: 1H structure, 15m volume, and a pullback zone."""
    pair = coin["pair"]
    hourly = get_candles(pair, "1hour", 36)
    fifteen = get_candles(pair, "15min", 100)
    if len(hourly) < 12 or len(fifteen) < 20:
        return 0, ["insufficient candles"]

    score, reasons = 0, []
    h = hourly[-1]
    h_closes = [c["close"] for c in hourly]
    avg6 = sum(h_closes[-6:]) / 6
    # Prefer improving hourly structure without requiring a late breakout.
    if h_closes[-1] >= h_closes[-2] and h_closes[-1] >= avg6:
        score += 2
        reasons.append("1H structure improving")
    if h["close"] > h["open"]:
        score += 1
        reasons.append("1H candle green")

    last = fifteen[-1]
    prior_vols = [c["volume"] for c in fifteen[-9:-1]]
    avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
    if avg_vol > 0 and last["volume"] >= avg_vol * 1.2:
        score += 2
        reasons.append("15M volume rising")
    if last["close"] >= last["open"]:
        score += 1
        reasons.append("15M candle green")

    recent = fifteen[-96:] if len(fifteen) >= 96 else fifteen
    low_24h = min(c["low"] for c in recent)
    high_24h = max(c["high"] for c in recent)
    price = coin["price"]
    above_low = ((price / low_24h) - 1) * 100 if low_24h > 0 else 999
    below_high = ((high_24h / price) - 1) * 100 if price > 0 else 0
    # Favor a retest near the low; avoid chasing near the 24h high.
    if 0 <= above_low <= 15:
        score += 2
        reasons.append(f"near 24H low (+{above_low:.1f}%)")
    elif below_high >= 8:
        score += 1
        reasons.append("not at 24H high")
    else:
        reasons.append("near 24H high—avoid chasing")

    # Penalize a sharp 24h move so the scanner doesn't chase an already-pumped coin.
    if coin["change"] >= 80:
        score -= 2
        reasons.append("already pumped hard")

    # Reject obvious sell-heavy books. This is a snapshot, not reliable bot detection.
    try:
        book_score, book_reason = orderbook_check(pair)
        reasons.append(book_reason)
        if book_score is not None:
            score += book_score
            if book_score <= -2:
                reasons.append("sell-pressure veto")
    except Exception as exc:
        print(f"Order book check failed for {pair}: {exc}")
        reasons.append("order book unavailable")
    return score, reasons


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

    # Candidate pool: deposit-off coins among top gainers, with narrative matches ranked first.
    # Market cap is not consistently available from KuCoin public APIs; avoid obvious large caps
    # and very high-turnover pairs, but do not pretend this proves <$650K market cap.
    LARGE_CAPS = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "TON", "LINK", "AVAX", "DOT", "LTC", "BCH", "SUI", "XLM", "SHIB", "PEPE"}
    top_gainers = gainers[:100]
    by_base = {c["base"]: c for c in gainers}
    watch = []
    for base in off_coins:
        if base in st_coins or base in LARGE_CAPS or base not in by_base:
            continue
        coin = by_base[base]
        # Exclude obvious large/liquid coins as call candidates; turnover is only a proxy.
        if coin["volume_usd"] > 10_000_000:
            continue
        narrative = classify_narrative(coin["name"], base)
        same_narrative = narrative in active_narratives
        top_rank = next((i + 1 for i, item in enumerate(top_gainers) if item["base"] == base), 999)
        # Include deposit-off top-gainers even if their narrative doesn't match the active theme.
        if same_narrative or top_rank <= 30:
            watch.append({"coin": coin, "narrative": narrative, "same_narrative": same_narrative, "top_rank": top_rank})

    # First evaluate technical setup; sort by confluence, narrative match, rank, and turnover.
    evaluated = []
    for item in watch:
        try:
            score, reasons = setup_check(item["coin"])
            item["score"] = score
            item["reasons"] = reasons
            evaluated.append(item)
        except Exception as exc:
            print(f"Candle check failed for {item['coin']['pair']}: {exc}")
    evaluated.sort(key=lambda x: (x["score"], x["same_narrative"], -x["top_rank"], x["coin"]["volume_usd"]), reverse=True)
    watch = evaluated[:20]

    # Daily shortlist of up to 6 best candidates, not a promise that 6 valid setups exist.
    new_watch = []
    prior_watch = set(state.get("watchlist", []))
    current_watch = {item["coin"]["base"] for item in watch}
    for item in watch:
        if item["coin"]["base"] not in prior_watch:
            new_watch.append(item)
    if new_watch:
        lines = ["🔎 DEPOSIT-OFF / NARRATIVE WATCHLIST"]
        lines.append("Top-gainer narrative(s): " + (", ".join(sorted(active_narratives)) or "unclear"))
        for item in new_watch[:6]:
            coin = item["coin"]
            lines.append(f"• {coin['pair']} | {item['narrative']} | 24H {coin['change']:+.1f}% | Top gainers #{item['top_rank']} | score {item['score']}/8")
        lines.append("Market cap is not verified by KuCoin API; big/liquid coins are filtered conservatively.")
        telegram("\n".join(lines))
    state["watchlist"] = sorted(current_watch)

    # Alert the best qualifying setups. Avoid repeating the same coin more than every 6 hours.
    setup_alerts = state.get("setup_alerts", {})
    ready_items = [
        item for item in watch
        if item["score"] >= 5
        and not any("near 24H high—avoid chasing" in r for r in item["reasons"])
        and not any("sell-pressure veto" in r for r in item["reasons"])
    ]
    ready_items = ready_items[:6]
    for item in ready_items:
        coin = item["coin"]
        now = int(time.time())
        last_alert = int(setup_alerts.get(coin["base"], 0) or 0)
        if now - last_alert >= 6 * 3600:
            match_text = "same narrative" if item["same_narrative"] else "deposit-off top gainer"
            telegram(
                "🚨 TOP URGENT SETUP\n"
                f"{coin['pair']} | {item['narrative']}\n"
                f"24H: {coin['change']:+.1f}% | Top gainer #{item['top_rank']}\n"
                f"Price: {coin['price']} | Setup score: {item['score']} (chart + order book)\n"
                f"Why: {', '.join(item['reasons'])}\n"
                f"Context: deposit OFF + {match_text}\n"
                "⚠️ Deposit OFF is not a buy signal; verify liquidity/news. No return guarantee."
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
        lines.append(f"Deposit-off candidates scored: {len(watch)} | shortlist target: up to 6")
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
