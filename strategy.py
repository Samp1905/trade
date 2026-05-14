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

SELECTOR_TTL = 60
LOOKAHEAD    = 3
MIN_HISTORY  = 35

# ------------------------------------------------------------------ #
# Indicators                                                          #
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


def _trend(closes: List[float], lookback: int = 5) -> int:
    """Returns +1 (uptrend), -1 (downtrend), 0 (flat) over last N closes."""
    if len(closes) < lookback + 1:
        return 0
    window = closes[-lookback:]
    ups = sum(1 for i in range(1, len(window)) if window[i] > window[i - 1])
    downs = sum(1 for i in range(1, len(window)) if window[i] < window[i - 1])
    if ups >= lookback - 1:
        return 1
    if downs >= lookback - 1:
        return -1
    return 0

# ------------------------------------------------------------------ #
# Strategies                                                          #
# ------------------------------------------------------------------ #

def strategy_trend_ema(closes: List[float]) -> Signal:
    """EMA crossover confirmed by short-term price trend — filters false crossovers."""
    if len(closes) < 23:
        return "HOLD"
    fp, sp = _ema(closes[:-1], 9), _ema(closes[:-1], 21)
    fc, sc = _ema(closes, 9),      _ema(closes, 21)
    rsi = _rsi(closes)
    t = _trend(closes, 5)
    if fp <= sp and fc > sc and rsi < 60 and t == 1:
        return "BUY"
    if fp >= sp and fc < sc and rsi > 40 and t == -1:
        return "SELL"
    return "HOLD"


def strategy_ema_crossover(closes: List[float]) -> Signal:
    """9/21 EMA crossover with RSI filter."""
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
    """RSI extremes with trend confirmation — avoids catching falling knives."""
    if len(closes) < 16:
        return "HOLD"
    rsi = _rsi(closes)
    t = _trend(closes, 3)
    if rsi < 32 and t == 1:    # oversold AND starting to recover
        return "BUY"
    if rsi > 68 and t == -1:   # overbought AND starting to drop
        return "SELL"
    return "HOLD"


def strategy_macd(closes: List[float]) -> Signal:
    """MACD line crossing zero — momentum shift."""
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
    """Bollinger Bands mean reversion with trend gate."""
    if len(closes) < period + 1:
        return "HOLD"
    window = closes[-period:]
    mean = sum(window) / period
    std  = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
    if std == 0:
        return "HOLD"
    price = closes[-1]
    t = _trend(closes, 3)
    if price < mean - 2 * std and t == 1:
        return "BUY"
    if price > mean + 2 * std and t == -1:
        return "SELL"
    return "HOLD"


STRATEGIES: Dict[str, Callable] = {
    "Trend+EMA":      strategy_trend_ema,
    "EMA Crossover":  strategy_ema_crossover,
    "RSI":            strategy_rsi,
    "MACD":           strategy_macd,
    "Bollinger Bands": strategy_bollinger,
}

# ------------------------------------------------------------------ #
# Auto-selector                                                       #
# ------------------------------------------------------------------ #

_active: str      = "Trend+EMA"
_last_eval: float = 0.0


def _score(fn: Callable, closes: List[float]) -> float:
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
