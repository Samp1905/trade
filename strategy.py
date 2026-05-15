"""
5-strategy auto-selector — tuned for active 1m scalping on Kraken Futures demo.
Strategies use VWAP, StochRSI, Supertrend, Bollinger+VWAP, and Donchian.
Every 60s each is back-tested on the last 100 candles; the best wins.
"""
import logging
import time
from typing import Callable, Dict, List, Literal, Optional

Signal = Literal["BUY", "SELL", "HOLD"]

logger = logging.getLogger(__name__)

SELECTOR_TTL = 60
LOOKAHEAD    = 3
MIN_HISTORY  = 40

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


def _stoch_rsi(closes: List[float], k_smooth: int = 3, d_smooth: int = 3,
               rsi_len: int = 14, stoch_len: int = 14):
    needed = rsi_len + stoch_len + k_smooth + d_smooth + 2
    if len(closes) < needed:
        return 50.0, 50.0
    rsi_series = [_rsi(closes[:i], rsi_len) for i in range(rsi_len + 1, len(closes) + 1)]
    stoch = []
    for i in range(stoch_len, len(rsi_series) + 1):
        w = rsi_series[i - stoch_len:i]
        mn, mx = min(w), max(w)
        stoch.append(0.0 if mx == mn else (rsi_series[i - 1] - mn) / (mx - mn) * 100)
    if len(stoch) < k_smooth + d_smooth:
        return 50.0, 50.0
    k_vals = [sum(stoch[i - k_smooth:i]) / k_smooth
              for i in range(k_smooth, len(stoch) + 1)]
    if len(k_vals) < d_smooth:
        return 50.0, 50.0
    return k_vals[-1], sum(k_vals[-d_smooth:]) / d_smooth


def _vwap(ohlcv: List) -> float:
    cum_tv, cum_v = 0.0, 0.0
    for c in ohlcv:
        tp = (c[2] + c[3] + c[4]) / 3
        v  = c[5]
        cum_tv += tp * v
        cum_v  += v
    return cum_tv / cum_v if cum_v > 0 else (ohlcv[-1][4] if ohlcv else 0.0)


def _sma(closes: List[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    return sum(closes[-period:]) / period


def _supertrend(ohlcv: List, period: int = 10, mult: float = 3.0) -> str:
    if len(ohlcv) < period + 2:
        return "up"
    highs  = [c[2] for c in ohlcv]
    lows   = [c[3] for c in ohlcv]
    closes = [c[4] for c in ohlcv]
    n = len(ohlcv)
    tr_list = [highs[0] - lows[0]]
    for i in range(1, n):
        tr_list.append(max(highs[i] - lows[i],
                           abs(highs[i] - closes[i - 1]),
                           abs(lows[i]  - closes[i - 1])))
    atr_list = [sum(tr_list[:period]) / period]
    for i in range(period, n):
        atr_list.append((atr_list[-1] * (period - 1) + tr_list[i]) / period)
    direction = 1
    prev_upper = prev_lower = 0.0
    for i in range(period - 1, n):
        atr   = atr_list[i - period + 1]
        hl2   = (highs[i] + lows[i]) / 2
        b_up  = hl2 + mult * atr
        b_lo  = hl2 - mult * atr
        if i == period - 1:
            f_up, f_lo = b_up, b_lo
        else:
            f_up = b_up if (b_up < prev_upper or closes[i - 1] > prev_upper) else prev_upper
            f_lo = b_lo if (b_lo > prev_lower or closes[i - 1] < prev_lower) else prev_lower
        if closes[i] > f_up:
            direction = 1
        elif closes[i] < f_lo:
            direction = -1
        prev_upper, prev_lower = f_up, f_lo
    return "up" if direction == 1 else "down"

# ------------------------------------------------------------------ #
# Strategies                                                          #
# ------------------------------------------------------------------ #

def strategy_vwap_ema_rsi(ohlcv: List, _=None) -> Signal:
    """
    VWAP bias + EMA9/21 alignment.
    Fires whenever price and both EMAs agree on direction relative to VWAP.
    """
    if len(ohlcv) < 25:
        return "HOLD"
    closes = [c[4] for c in ohlcv]
    vwap   = _vwap(ohlcv)
    price  = closes[-1]
    e9     = _ema(closes, 9)
    e21    = _ema(closes, 21)
    if price > vwap and e9 > e21:
        return "BUY"
    if price < vwap and e9 < e21:
        return "SELL"
    return "HOLD"


def strategy_stoch_rsi_ema50(ohlcv: List, _=None) -> Signal:
    """StochRSI extreme zones filtered by EMA 50 bias."""
    if len(ohlcv) < 55:
        return "HOLD"
    closes = [c[4] for c in ohlcv]
    ema50  = _ema(closes, 50)
    price  = closes[-1]
    k, _   = _stoch_rsi(closes)
    if price > ema50 and k < 25:     # oversold in uptrend
        return "BUY"
    if price < ema50 and k > 75:     # overbought in downtrend
        return "SELL"
    return "HOLD"


def strategy_supertrend_mtf(ohlcv: List, ohlcv_5m: Optional[List] = None) -> Signal:
    """Supertrend(10,3) flip on 1m, optionally confirmed by 5m direction."""
    if len(ohlcv) < 15:
        return "HOLD"
    st_now  = _supertrend(ohlcv)
    st_prev = _supertrend(ohlcv[:-1])
    if st_now == st_prev:
        return "HOLD"
    if ohlcv_5m and len(ohlcv_5m) >= 15:
        if _supertrend(ohlcv_5m) != st_now:
            return "HOLD"
    return "BUY" if st_now == "up" else "SELL"


def strategy_vwap_bb_reversion(ohlcv: List, _=None) -> Signal:
    """Price beyond 1.8σ Bollinger Band and on the VWAP side — mean reversion."""
    if len(ohlcv) < 22:
        return "HOLD"
    closes = [c[4] for c in ohlcv]
    vwap   = _vwap(ohlcv)
    price  = closes[-1]
    w      = closes[-20:]
    mean   = sum(w) / 20
    std    = (sum((x - mean) ** 2 for x in w) / 20) ** 0.5
    if std == 0:
        return "HOLD"
    if price < mean - 1.8 * std and price > vwap * 0.998:
        return "BUY"
    if price > mean + 1.8 * std and price < vwap * 1.002:
        return "SELL"
    return "HOLD"


def strategy_donchian_breakout(ohlcv: List, _=None) -> Signal:
    """Donchian(20) channel breakout in direction of EMA 50."""
    if len(ohlcv) < 55:
        return "HOLD"
    closes = [c[4] for c in ohlcv]
    ema50  = _ema(closes, 50)
    price  = closes[-1]
    prev   = closes[-2]
    upper  = max(closes[-21:-1])
    lower  = min(closes[-21:-1])
    if prev <= upper and price > upper and price > ema50:
        return "BUY"
    if prev >= lower and price < lower and price < ema50:
        return "SELL"
    return "HOLD"


def strategy_sma200_rsi10(ohlcv: List, _=None) -> Signal:
    """
    SMA 200 trend filter + RSI(10) oversold entry.
    Bullish-only: only BUY when price > SMA(200) and RSI(10) < 30.
    Exit is handled separately (RSI cross above 40, swing-low stop loss).
    """
    if len(ohlcv) < 201:
        return "HOLD"
    closes = [c[4] for c in ohlcv]
    sma200 = _sma(closes, 200)
    price  = closes[-1]
    if price <= sma200:
        return "HOLD"           # price below SMA 200 — no trades
    if _rsi(closes, 10) < 30:
        return "BUY"            # oversold pullback inside uptrend
    return "HOLD"


STRATEGIES: Dict[str, Callable] = {
    "SMA200+RSI10":      strategy_sma200_rsi10,
    "VWAP+EMA+RSI":      strategy_vwap_ema_rsi,
    "StochRSI+EMA50":    strategy_stoch_rsi_ema50,
    "Supertrend MTF":    strategy_supertrend_mtf,
    "VWAP+BB Reversion": strategy_vwap_bb_reversion,
    "Donchian Breakout": strategy_donchian_breakout,
}

# ------------------------------------------------------------------ #
# Auto-selector                                                       #
# ------------------------------------------------------------------ #

_active: str      = "VWAP+EMA+RSI"
_last_eval: float = 0.0


def _score(fn: Callable, ohlcv: List, ohlcv_5m: Optional[List] = None) -> float:
    closes = [c[4] for c in ohlcv]
    wins = losses = 0
    for i in range(MIN_HISTORY, len(ohlcv) - LOOKAHEAD):
        sig = fn(ohlcv[:i], ohlcv_5m)
        if sig == "HOLD":
            continue
        entry  = closes[i]
        future = closes[i + LOOKAHEAD]
        if (future > entry) if sig == "BUY" else (future < entry):
            wins += 1
        else:
            losses += 1
    total = wins + losses
    return wins / total if total >= 5 else 0.0


def _reselect(ohlcv: List, ohlcv_5m: Optional[List] = None) -> None:
    global _active
    scores = {name: _score(fn, ohlcv, ohlcv_5m) for name, fn in STRATEGIES.items()}
    best   = max(scores, key=scores.__getitem__)
    summary = "  |  ".join(f"{k}: {v:.0%}" for k, v in scores.items())
    logger.info(f"Strategy scores — {summary}  →  using {best}")
    _active = best


def get_signal(ohlcv: List, ohlcv_5m: Optional[List] = None) -> Signal:
    global _last_eval
    now = time.time()
    if now - _last_eval > SELECTOR_TTL and len(ohlcv) >= MIN_HISTORY + LOOKAHEAD:
        _reselect(ohlcv, ohlcv_5m)
        _last_eval = now

    # Primary: best-scored strategy
    sig = STRATEGIES[_active](ohlcv, ohlcv_5m)
    if sig != "HOLD":
        return sig

    # Fallback: scan remaining strategies — first non-HOLD wins
    for name, fn in STRATEGIES.items():
        if name == _active:
            continue
        s = fn(ohlcv, ohlcv_5m)
        if s != "HOLD":
            return s
    return "HOLD"


def active_strategy() -> str:
    return _active
