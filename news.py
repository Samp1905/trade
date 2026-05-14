"""
Market scanner — no API key required.
Uses CoinGecko free API to find coins with strong momentum (trending + 1h movers).
These moves are typically news-driven.
"""
import logging
import requests
from typing import Dict

logger = logging.getLogger(__name__)

COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_MARKETS  = "https://api.coingecko.com/api/v3/coins/markets"

# CoinGecko ID → Kraken ticker
COIN_MAP = {
    "bitcoin":      "BTC",
    "ethereum":     "ETH",
    "solana":       "SOL",
    "ripple":       "XRP",
    "dogecoin":     "DOGE",
    "cardano":      "ADA",
    "avalanche-2":  "AVAX",
    "chainlink":    "LINK",
    "polkadot":     "DOT",
    "litecoin":     "LTC",
    "bitcoin-cash": "BCH",
    "uniswap":      "UNI",
    "cosmos":       "ATOM",
    "matic-network":"MATIC",
}

MOVE_THRESHOLD_PCT = 1.5   # 1h price change to qualify as a mover


def get_news_signals(_api_key: str = "") -> Dict[str, str]:
    """Return {coin: 'BUY'/'SELL'} — free, no API key needed."""
    signals: Dict[str, str] = {}

    # 1. Trending coins on CoinGecko (search-volume surge = news interest)
    try:
        data = requests.get(COINGECKO_TRENDING, timeout=8).json()
        for item in data.get("coins", []):
            coin_id = item.get("item", {}).get("id", "")
            ticker = COIN_MAP.get(coin_id)
            if ticker:
                signals[ticker] = "BUY"
    except Exception as e:
        logger.warning(f"Trending fetch failed: {e}")

    # 2. 1-hour price movers (strong momentum = often news-driven)
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
        logger.warning(f"Market movers fetch failed: {e}")

    if signals:
        logger.info(f"Market signals: {signals}")
    return signals
