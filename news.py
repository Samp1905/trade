import logging
import requests
from typing import Dict

logger = logging.getLogger(__name__)

CRYPTOPANIC_URL = "https://cryptopanic.com/api/free/v1/posts/"

# Coins available as perpetuals on Kraken Futures
KRAKEN_COINS = {
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX",
    "LINK", "DOT", "LTC", "BCH", "UNI", "ATOM", "MATIC",
}


def get_news_signals(api_key: str) -> Dict[str, str]:
    """Return {coin: 'BUY'/'SELL'} for coins with strong bullish/bearish news."""
    try:
        resp = requests.get(
            CRYPTOPANIC_URL,
            params={"auth_token": api_key, "filter": "hot", "kind": "news"},
            timeout=8,
        )
        resp.raise_for_status()
        posts = resp.json().get("results", [])
    except Exception as e:
        logger.warning(f"News fetch failed: {e}")
        return {}

    scores: Dict[str, int] = {}
    for post in posts:
        votes = post.get("votes", {})
        score = votes.get("positive", 0) - votes.get("negative", 0)
        for currency in post.get("currencies", []):
            code = currency.get("code", "").upper()
            if code in KRAKEN_COINS:
                scores[code] = scores.get(code, 0) + score

    signals = {}
    for coin, score in scores.items():
        if score >= 3:
            signals[coin] = "BUY"
        elif score <= -3:
            signals[coin] = "SELL"

    if signals:
        logger.info(f"News signals: {signals}")
    return signals
