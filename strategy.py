"""
Strategy module — swap this function to change the trading signal.
Input:  list of 1-minute closing prices (oldest → newest)
Output: "BUY" | "SELL" | "HOLD"

Strategy: EMA 9/21 crossover with RSI filter
- BUY  when 9-EMA crosses above 21-EMA and RSI < 65 (not overbought)
- SELL when 9-EMA crosses below 21-EMA and RSI > 35 (not oversold)
- HOLD otherwise
"""
from typing import List, Literal

Signal = Literal["BUY", "SELL", "HOLD"]

FAST_EMA = 9
SLOW_EMA = 21
RSI_PERIOD = 14
RSI_MAX_LONG = 65   # don't buy into overbought conditions
RSI_MIN_SHORT = 35  # don't sell into oversold conditions


def _ema(closes: List[float], period: int) -> float:
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _rsi(closes: List[float], period: int = RSI_PERIOD) -> float:
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def get_signal(closes: List[float]) -> Signal:
    if len(closes) < SLOW_EMA + 2:
        return "HOLD"

    fast_prev = _ema(closes[:-1], FAST_EMA)
    slow_prev = _ema(closes[:-1], SLOW_EMA)
    fast_curr = _ema(closes, FAST_EMA)
    slow_curr = _ema(closes, SLOW_EMA)
    rsi = _rsi(closes)

    bullish_cross = fast_prev <= slow_prev and fast_curr > slow_curr
    bearish_cross = fast_prev >= slow_prev and fast_curr < slow_curr

    if bullish_cross and rsi < RSI_MAX_LONG:
        return "BUY"
    if bearish_cross and rsi > RSI_MIN_SHORT:
        return "SELL"
    return "HOLD"
