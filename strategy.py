"""
Momentum Scalper — high-conviction single-position entries.

Entry logic:
  BUY  → Triple EMA aligned up (EMA5 > EMA13 > EMA21)
          + RSI(7) in momentum zone (45–72)
          + MACD histogram positive AND rising (bullish pressure building)
          + Strong green candle body (≥ 50% of candle range)

  SELL → Triple EMA aligned down (EMA5 < EMA13 < EMA21)
          + RSI(7) in momentum zone (28–55)
          + MACD histogram negative AND falling
          + Strong red candle body (≥ 50% of candle range)

All four conditions must agree — no exceptions, no fallback.
"""
import logging
from typing import List, Literal, Optional

Signal = Literal["BUY", "SELL", "HOLD"]

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Indicators                                                          #
# ------------------------------------------------------------------ #

def _ema(closes: List[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
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


def _macd(closes: List[float], fast: int = 12, slow: int = 26,
          sig_len: int = 9):
    """Returns (macd, signal, histogram, prev_histogram)."""
    if len(closes) < slow + sig_len + 2:
        return 0.0, 0.0, 0.0, 0.0
    k_f = 2 / (fast + 1)
    k_s = 2 / (slow + 1)
    ema_f = sum(closes[:fast]) / fast
    ema_s = sum(closes[:slow]) / slow
    for i in range(fast, slow):
        ema_f = closes[i] * k_f + ema_f * (1 - k_f)
    macd_line: List[float] = []
    for i in range(slow, len(closes)):
        ema_f = closes[i] * k_f + ema_f * (1 - k_f)
        ema_s = closes[i] * k_s + ema_s * (1 - k_s)
        macd_line.append(ema_f - ema_s)
    if len(macd_line) < sig_len + 1:
        return 0.0, 0.0, 0.0, 0.0
    k_sig = 2 / (sig_len + 1)

    def _sig_at(idx: int) -> float:
        s = sum(macd_line[:sig_len]) / sig_len
        for m in macd_line[sig_len:idx + 1]:
            s = m * k_sig + s * (1 - k_sig)
        return s

    sig_now  = _sig_at(len(macd_line) - 1)
    sig_prev = _sig_at(len(macd_line) - 2)
    hist     = macd_line[-1] - sig_now
    prev_hist = macd_line[-2] - sig_prev
    return macd_line[-1], sig_now, hist, prev_hist


def _candle_body_pct(candle) -> float:
    """Fraction of the candle range that is body. 0 = doji, 1 = full body."""
    o, h, l, c = candle[1], candle[2], candle[3], candle[4]
    rng = h - l
    return abs(c - o) / rng if rng > 0 else 0.0


def _vwap(ohlcv: List) -> float:
    cum_tv, cum_v = 0.0, 0.0
    for c in ohlcv:
        tp = (c[2] + c[3] + c[4]) / 3
        cum_tv += tp * c[5]
        cum_v  += c[5]
    return cum_tv / cum_v if cum_v > 0 else (ohlcv[-1][4] if ohlcv else 0.0)


# ------------------------------------------------------------------ #
# Signal strength scorer (higher = more confident entry)             #
# ------------------------------------------------------------------ #

def signal_strength(ohlcv: List) -> float:
    """
    Returns a score 0–4 indicating how strongly this coin is setting up.
    Used to pick the BEST coin when scanning multiple.
    """
    if len(ohlcv) < 40:
        return 0.0
    closes = [c[4] for c in ohlcv]
    e5  = _ema(closes, 5)
    e13 = _ema(closes, 13)
    e21 = _ema(closes, 21)
    rsi = _rsi(closes, 7)
    _, _, hist, prev_hist = _macd(closes)
    body = _candle_body_pct(ohlcv[-1])

    bull_score = (
        float(e5 > e13 > e21) +
        float(45 <= rsi <= 72) +
        float(hist > 0 and hist > prev_hist) +
        float(body >= 0.50)
    )
    bear_score = (
        float(e5 < e13 < e21) +
        float(28 <= rsi <= 55) +
        float(hist < 0 and hist < prev_hist) +
        float(body >= 0.50)
    )
    return max(bull_score, bear_score)


# ------------------------------------------------------------------ #
# Main signal                                                         #
# ------------------------------------------------------------------ #

def get_signal(ohlcv: List, _=None) -> Signal:
    """
    Fires BUY or SELL only when all 4 momentum conditions agree.
    Returns HOLD otherwise — patience is part of the strategy.
    """
    if len(ohlcv) < 40:
        return "HOLD"

    closes = [c[4] for c in ohlcv]
    e5  = _ema(closes, 5)
    e13 = _ema(closes, 13)
    e21 = _ema(closes, 21)
    rsi = _rsi(closes, 7)
    _, _, hist, prev_hist = _macd(closes)
    body = _candle_body_pct(ohlcv[-1])

    if (e5 > e13 > e21
            and 45 <= rsi <= 72
            and hist > 0 and hist > prev_hist
            and body >= 0.50):
        logger.debug(f"BUY  e5={e5:.4f} e13={e13:.4f} e21={e21:.4f} "
                     f"rsi={rsi:.1f} hist={hist:.6f} body={body:.2f}")
        return "BUY"

    if (e5 < e13 < e21
            and 28 <= rsi <= 55
            and hist < 0 and hist < prev_hist
            and body >= 0.50):
        logger.debug(f"SELL e5={e5:.4f} e13={e13:.4f} e21={e21:.4f} "
                     f"rsi={rsi:.1f} hist={hist:.6f} body={body:.2f}")
        return "SELL"

    return "HOLD"


def active_strategy() -> str:
    return "Momentum Scalper"
