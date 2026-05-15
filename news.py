"""
News + market signal scanner — no API keys required.
Sources:
  1. CoinTelegraph RSS  — headline sentiment per coin
  2. CoinDesk RSS       — headline sentiment per coin
  3. CoinGecko trending — search-volume surge (news interest)
  4. CoinGecko 1h movers — price momentum (often news-driven)
  5. Fear & Greed Index  — market-wide sentiment filter
"""
import json
import logging
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Tuple

import requests

logger = logging.getLogger(__name__)

# CoinGecko ID → Kraken ticker
# Only coins that trade well on Kraken Futures with clean signals.
# BTC/DOGE/LINK/XRP/ATOM removed — they were the largest loss sources.
COIN_MAP = {
    "ethereum":     "ETH",
    "solana":       "SOL",
    "cardano":      "ADA",
    "polkadot":     "DOT",
    "uniswap":      "UNI",
    "litecoin":     "LTC",
}

# Text keywords for each coin (used in headline scanning)
_COIN_KEYWORDS: Dict[str, set] = {
    "ETH":  {"ethereum", "eth", "ether"},
    "SOL":  {"solana", "sol"},
    "ADA":  {"cardano", "ada"},
    "DOT":  {"polkadot", "dot"},
    "UNI":  {"uniswap", "uni"},
    "LTC":  {"litecoin", "ltc"},
}

_POSITIVE = {
    "surge", "surges", "rally", "rallies", "soar", "soars", "gain", "gains",
    "rise", "rises", "pump", "pumps", "breakout", "adoption", "partnership",
    "launch", "launches", "record", "high", "growth", "bullish", "upgrade",
    "milestone", "integration", "approve", "approved", "listing", "listed",
}
_NEGATIVE = {
    "crash", "crashes", "dump", "dumps", "plunge", "plunges", "drop", "drops",
    "fall", "falls", "hack", "hacked", "ban", "banned", "lawsuit", "concern",
    "selloff", "decline", "declines", "warning", "fear", "bearish", "collapse",
    "exploit", "breach", "scam", "fraud", "delist", "delisted", "probe",
}

_RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]

COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_MARKETS  = "https://api.coingecko.com/api/v3/coins/markets"
FEAR_GREED_URL     = "https://api.alternative.me/fng/?limit=1"
MOVE_THRESHOLD_PCT = 3.0   # raised from 1.5 — require stronger move to signal


def get_fear_greed() -> Tuple[int, str]:
    """Returns (value 0-100, label). 0=extreme fear, 100=extreme greed."""
    try:
        with urllib.request.urlopen(FEAR_GREED_URL, timeout=5) as r:
            data = json.loads(r.read())
        entry = data["data"][0]
        return int(entry["value"]), entry["value_classification"]
    except Exception:
        return 50, "Neutral"


def _rss_sentiment() -> Dict[str, int]:
    """Scan RSS headlines — returns {coin: net_sentiment_score}."""
    scores: Dict[str, int] = {c: 0 for c in _COIN_KEYWORDS}
    for url in _RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                content = r.read().decode("utf-8", errors="ignore")
            root = ET.fromstring(content)
            items = root.findall(".//item")
            for item in items[:20]:
                parts = []
                for tag in ("title", "description"):
                    el = item.find(tag)
                    if el is not None and el.text:
                        parts.append(el.text.lower())
                text = " ".join(parts)
                words = set(text.split())
                sentiment = len(words & _POSITIVE) - len(words & _NEGATIVE)
                for coin, keywords in _COIN_KEYWORDS.items():
                    if any(kw in text for kw in keywords):
                        scores[coin] += sentiment
        except Exception as e:
            logger.debug(f"RSS {url}: {e}")
    return scores


def _coingecko_signals() -> Dict[str, str]:
    signals: Dict[str, str] = {}
    try:
        data = requests.get(COINGECKO_TRENDING, timeout=8).json()
        for item in data.get("coins", []):
            ticker = COIN_MAP.get(item.get("item", {}).get("id", ""))
            if ticker:
                signals[ticker] = "BUY"
    except Exception as e:
        logger.debug(f"CoinGecko trending: {e}")
    try:
        resp = requests.get(
            COINGECKO_MARKETS,
            params={
                "vs_currency": "usd",
                "ids": ",".join(COIN_MAP.keys()),
                "order": "market_cap_desc",
                "per_page": 50,
                "price_change_percentage": "1h",
            },
            timeout=8,
        )
        for coin in resp.json():
            ticker = COIN_MAP.get(coin.get("id", ""))
            if not ticker:
                continue
            change = coin.get("price_change_percentage_1h_in_currency") or 0.0
            if change >= MOVE_THRESHOLD_PCT:
                signals[ticker] = "BUY"
            elif change <= -MOVE_THRESHOLD_PCT:
                signals[ticker] = "SELL"
    except Exception as e:
        logger.debug(f"CoinGecko movers: {e}")
    return signals


def get_news_signals(_api_key: str = "") -> Dict[str, str]:
    """
    Combined news signal from RSS sentiment + CoinGecko + Fear & Greed filter.
    Returns {coin: 'BUY'/'SELL'}.
    """
    signals: Dict[str, str] = {}

    # 1. RSS headline sentiment (CoinTelegraph + CoinDesk)
    rss_scores = _rss_sentiment()
    for coin, score in rss_scores.items():
        if score >= 2:
            signals[coin] = "BUY"
        elif score <= -2:
            signals[coin] = "SELL"

    # 2. CoinGecko price momentum (only CONFIRMS existing RSS signals)
    # CoinGecko cannot add new coins on its own — this prevents noise coins
    # like BTC (always trending) from flooding the watchlist.
    cg = _coingecko_signals()
    for coin in list(signals.keys()):
        cg_sig = cg.get(coin)
        if cg_sig is not None and cg_sig != signals[coin]:
            del signals[coin]   # RSS and CG disagree → drop

    # 3. Fear & Greed filter — extreme readings suppress counter-sentiment trades
    fg_val, fg_label = get_fear_greed()
    if fg_val <= 25:       # extreme fear — strip BUY signals
        signals = {c: s for c, s in signals.items() if s == "SELL"}
        logger.info(f"Fear & Greed = {fg_val} ({fg_label}) — BUY signals suppressed")
    elif fg_val >= 75:     # extreme greed — strip SELL signals
        signals = {c: s for c, s in signals.items() if s == "BUY"}
        logger.info(f"Fear & Greed = {fg_val} ({fg_label}) — SELL signals suppressed")

    if signals:
        logger.info(f"News signals (RSS+CG+F&G={fg_val}): {signals}")
    return signals
