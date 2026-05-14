"""
Multi-strategy auto-selector.
Every 60 seconds, each strategy is back-tested on the last 100 candles.
The one with the highest win rate is used for actual trades.
"""
import logging
import time
from typing import Callable, Dict, List, Literal

Signal = Literal["BUY", "SELL", "HOLD"]

logger = logging.getLogger(__name__)

SELECTOR_TTL   = 60    # re-score strategies every 60 seconds
LOOKAHEAD      = 3     # candles ahead to judge if a signal was correct
MIN_HISTORY    = 35    # minimum candles needed to score

# ------------------------------------------------------------------ #
# Shared indicators                                                   #
# ------------------------------------------------------------------ #

def _ema(closes: List[float], period: int) -> float:
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + ag / al))

# ------------------------------------------------------------------ #
# Strategies                                                          #
# ------------------------------------------------------------------ #

def strategy_ema_crossover(closes: List[float]) -> Signal:
    """9/21 EMA crossover with RSI filter — good in trending markets."""
    if len(closes) < 23:
        return "HOLD"
    fp, sp = _ema(closes[:-1], 9), _ema(closes[:-1], 21)
    fc, sc = _ema(closes, 9),      _ema(closes, 21)
    rsi = _rsi(closes)
    if fp <= sp and fc > sc and rsi < 65:
        return "BUY"
    if fp >= sp and fc < sc and rsi > 35:
        return "SELL"
    return "HOLD"


def strategy_rsi(closes: List[float]) -> Signal:
    """RSI extremes — good in ranging markets."""
    if len(closes) < 16:
        return "HOLD"
    rsi = _rsi(closes)
    if rsi < 30:
        return "BUY"
    if rsi > 70:
        return "SELL"
    return "HOLD"


def strategy_macd(closes: List[float]) -> Signal:
    """MACD line crossing zero — good for momentum shifts."""
    if len(closes) < 35:
        return "HOLD"
    macd_prev = _ema(closes[:-1], 12) - _ema(closes[:-1], 26)
    macd_curr = _ema(closes, 12)      - _ema(closes, 26)
    if macd_prev < 0 and macd_curr >= 0:
        return "BUY"
    if macd_prev > 0 and macd_curr <= 0:
        return "SELL"
    return "HOLD"


def strategy_bollinger(closes: List[float], period: int = 20) -> Signal:
    """Bollinger Bands mean reversion — good for choppy markets."""
    if len(closes) < period + 1:
        return "HOLD"
    window = closes[-period:]
    mean = sum(window) / period
    std  = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
    if std == 0:
        return "HOLD"
    price = closes[-1]
    if price < mean - 2 * std:
        return "BUY"
    if price > mean + 2 * std:
        return "SELL"
    return "HOLD"


STRATEGIES: Dict[str, Callable] = {
    "EMA Crossover":  strategy_ema_crossover,
    "RSI":            strategy_rsi,
    "MACD":           strategy_macd,
    "Bollinger Bands": strategy_bollinger,
}

# ------------------------------------------------------------------ #
# Auto-selector                                                       #
# ------------------------------------------------------------------ #

_active: str   = "EMA Crossover"
_last_eval: float = 0.0


def _score(fn: Callable, closes: List[float]) -> float:
    """Win rate of strategy on recent candle history (0.0 – 1.0)."""
    wins = losses = 0
    for i in range(MIN_HISTORY, len(closes) - LOOKAHEAD):
        sig = fn(closes[:i])
        if sig == "HOLD":
            continue
        entry, future = closes[i], closes[i + LOOKAHEAD]
        correct = (future > entry) if sig == "BUY" else (future < entry)
        if correct:
            wins += 1
        else:
            losses += 1
    total = wins + losses
    return wins / total if total >= 5 else 0.0


def _reselect(closes: List[float]) -> None:
    global _active
    scores = {name: _score(fn, closes) for name, fn in STRATEGIES.items()}
    best   = max(scores, key=scores.__getitem__)
    summary = "  |  ".join(f"{k}: {v:.0%}" for k, v in scores.items())
    logger.info(f"Strategy scores — {summary}  →  using {best}")
    _active = best


def get_signal(closes: List[float]) -> Signal:
    global _last_eval
    now = time.time()
    if now - _last_eval > SELECTOR_TTL and len(closes) >= MIN_HISTORY + LOOKAHEAD:
        _reselect(closes)
        _last_eval = now
    return STRATEGIES[_active](closes)


def active_strategy() -> str:
    return _active
