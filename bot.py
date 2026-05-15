import logging
import os
import time
from typing import Dict, List, Optional

import ccxt
from dotenv import load_dotenv

from news import get_news_signals, get_fear_greed
from state import bot_state
from strategy import get_signal, active_strategy, signal_strength

load_dotenv()

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Configuration                                                        #
# ------------------------------------------------------------------ #

POSITION_SIZE_USD    = 5000.0   # large notional per trade (~4x leverage)
KILL_SWITCH_DRAWDOWN = 0.10     # halt if down 10% from day open
LOOP_INTERVAL_SECS   = 10
TRADE_COOLDOWN_SECS  = 30       # 30s minimum between trades per coin
TAKE_PROFIT_PCT      = 0.005    # exit at +0.5% — quick scalp profit
EMERGENCY_STOP_PCT   = 0.03     # hard -3% emergency exit (no regular SL)
SIGNAL_REFRESH_SECS  = 10       # refresh OHLCV aggressively for scalping
NEWS_REFRESH_SECS    = 120
EQUITY_REFRESH_SECS  = 30

# Coins scanned for the best setup — bot picks ONE at a time
SCAN_COINS = ["SOL", "ETH", "ADA", "DOT", "UNI"]


def _sym(coin: str) -> str:
    return f"{coin}/USD:USD"


class TradingBot:
    def __init__(self) -> None:
        self.exchange = ccxt.krakenfutures({
            "apiKey": os.environ["KRAKEN_API_KEY"],
            "secret": os.environ["KRAKEN_API_SECRET"],
            "timeout": 30000,
        })
        self.exchange.set_sandbox_mode(True)
        for attempt in range(1, 6):
            try:
                self.exchange.load_markets()
                break
            except Exception as e:
                if attempt == 5:
                    raise
                wait = attempt * 10
                logger.warning(f"load_markets attempt {attempt} failed ({e}) — retrying in {wait}s")
                time.sleep(wait)

        self._day_open_equity: Optional[float] = None
        self._halted = False

        self._cached_equity: float = 0.0
        self._last_equity_time: float = 0.0
        self._last_trade_time: Dict[str, float] = {}
        self._last_news_refresh: float = 0.0
        self._news_signals: Dict[str, str] = {}

        self._ohlcv_cache: Dict[str, List] = {}
        self._last_ohlcv_time: Dict[str, float] = {}
        self._ohlcv_5m_cache: Dict[str, List] = {}
        self._last_ohlcv_5m_time: Dict[str, float] = {}

        # Current focused coin — bot trades only this one at a time
        self._focus_coin: Optional[str] = None

        bot_state.register_close(self._close_by_coin)

    # ------------------------------------------------------------------ #
    # Data helpers                                                         #
    # ------------------------------------------------------------------ #

    def _equity(self) -> float:
        now = time.time()
        if now - self._last_equity_time > EQUITY_REFRESH_SECS:
            try:
                self._cached_equity = float(
                    self.exchange.fetch_balance()["USD"]["total"]
                )
                self._last_equity_time = now
            except Exception:
                pass
        return self._cached_equity

    def _get_ohlcv(self, coin: str) -> List:
        now = time.time()
        if now - self._last_ohlcv_time.get(coin, 0) > SIGNAL_REFRESH_SECS:
            try:
                self._ohlcv_cache[coin] = self.exchange.fetch_ohlcv(
                    _sym(coin), "1m", limit=80
                )
                self._last_ohlcv_time[coin] = now
            except Exception as e:
                logger.debug(f"OHLCV 1m {coin}: {e}")
        return self._ohlcv_cache.get(coin, [])

    def _get_ohlcv_5m(self, coin: str) -> List:
        now = time.time()
        if now - self._last_ohlcv_5m_time.get(coin, 0) > 60:
            try:
                self._ohlcv_5m_cache[coin] = self.exchange.fetch_ohlcv(
                    _sym(coin), "5m", limit=40
                )
                self._last_ohlcv_5m_time[coin] = now
            except Exception as e:
                logger.debug(f"OHLCV 5m {coin}: {e}")
        return self._ohlcv_5m_cache.get(coin, [])

    def _fetch_bid_ask(self, sym: str) -> tuple:
        t = self.exchange.fetch_ticker(sym)
        last = float(t["last"] or 0)
        ask  = float(t["ask"] or last)
        bid  = float(t["bid"] or last)
        return last, ask, bid

    def _all_positions(self) -> Dict[str, dict]:
        result = {}
        try:
            for pos in self.exchange.fetch_positions():
                if pos.get("contracts") and float(pos["contracts"]) != 0:
                    coin = pos["symbol"].split("/")[0]
                    result[coin] = pos
        except Exception as e:
            logger.warning(f"fetch_positions error: {e}")
        return result

    # ------------------------------------------------------------------ #
    # 5m trend gate — never trade against the higher-timeframe trend      #
    # ------------------------------------------------------------------ #

    def _htf_trend_ok(self, coin: str, signal: str) -> bool:
        ohlcv_5m = self._get_ohlcv_5m(coin)
        if len(ohlcv_5m) < 25:
            return True
        from strategy import _ema as ema
        closes_5m = [c[4] for c in ohlcv_5m]
        e9  = ema(closes_5m, 9)
        e21 = ema(closes_5m, 21)
        if signal == "BUY"  and e9 > e21:
            return True
        if signal == "SELL" and e9 < e21:
            return True
        return False

    # ------------------------------------------------------------------ #
    # Coin scanner — pick the one with the strongest setup                #
    # ------------------------------------------------------------------ #

    def _best_coin(self) -> Optional[tuple]:
        """
        Scan SCAN_COINS for the highest signal_strength score.
        Returns (coin, signal, score) or None if nothing is firing.
        """
        best = None
        best_score = 0.0
        best_signal = "HOLD"

        available = [c for c in SCAN_COINS if _sym(c) in self.exchange.markets]
        for coin in available:
            try:
                ohlcv = self._get_ohlcv(coin)
                if len(ohlcv) < 40:
                    continue
                sig = get_signal(ohlcv)
                if sig == "HOLD":
                    continue
                score = signal_strength(ohlcv)
                if score < 3.5:        # require at least 3.5/4 conditions
                    continue
                if not self._htf_trend_ok(coin, sig):
                    logger.debug(f"{coin}: {sig} blocked by 5m trend")
                    continue
                if score > best_score:
                    best_score = score
                    best = coin
                    best_signal = sig
            except Exception as e:
                logger.debug(f"scan {coin}: {e}")

        return (best, best_signal, best_score) if best else None

    # ------------------------------------------------------------------ #
    # Order helpers                                                        #
    # ------------------------------------------------------------------ #

    def _place_order(self, sym: str, side: str, size: float,
                     ask: float, bid: float, params: dict = None) -> dict:
        is_buy = side == "buy"
        lp = float(self.exchange.price_to_precision(sym, ask if is_buy else bid))
        return (self.exchange.create_limit_buy_order(sym, size, lp, params=params or {})
                if is_buy else
                self.exchange.create_limit_sell_order(sym, size, lp, params=params or {}))

    def _cancel_open_orders(self, coin: str) -> None:
        sym = _sym(coin)
        try:
            for o in self.exchange.fetch_open_orders(sym):
                try:
                    self.exchange.cancel_order(o["id"], sym)
                except Exception:
                    pass
        except Exception:
            pass

    def _enter(self, coin: str, signal: str, price: float) -> None:
        sym  = _sym(coin)
        size = float(self.exchange.amount_to_precision(sym, POSITION_SIZE_USD / price))
        self._cancel_open_orders(coin)
        last, ask, bid = self._fetch_bid_ask(sym)
        is_buy = signal == "BUY"
        logger.info(f"ENTER {'LONG' if is_buy else 'SHORT'} {size} {coin} @ ~${last:.4f}  "
                    f"(${POSITION_SIZE_USD:.0f} notional — no SL — TP +{TAKE_PROFIT_PCT*100:.2f}%)")
        result = self._place_order(sym, "buy" if is_buy else "sell", size, ask, bid)
        self._last_trade_time[coin] = time.time()
        self._focus_coin = coin
        bot_state.add_trade({
            "time": time.time(), "action": "ENTER", "coin": coin,
            "side": "long" if is_buy else "short",
            "size": size, "price": last, "order_id": result.get("id"),
        })
        logger.info(f"Order {result.get('id')} {result.get('status')}")

    def _exit(self, coin: str, pos: dict, reason: str = "EXIT") -> None:
        sym  = _sym(coin)
        size = abs(float(pos["contracts"]))
        side = pos["side"]
        self._cancel_open_orders(coin)
        last, ask, bid = self._fetch_bid_ask(sym)
        exit_side = "sell" if side == "long" else "buy"
        result = self._place_order(sym, exit_side, size, ask, bid, {"reduceOnly": True})
        logger.info(f"{reason} {side.upper()} {size} {coin} @ ~${last:.4f}")
        bot_state.add_trade({
            "time": time.time(), "action": reason, "coin": coin,
            "side": side, "size": size, "price": last,
            "order_id": result.get("id"),
        })
        if self._focus_coin == coin:
            self._focus_coin = None

    def _close_by_coin(self, coin: str) -> dict:
        positions = self._all_positions()
        if coin == "__all__":
            closed = []
            for c, pos in positions.items():
                self._exit(c, pos, reason="MANUAL")
                closed.append(c)
            return {"closed": closed}
        if coin not in positions:
            return {"error": f"no open position for {coin}"}
        self._exit(coin, positions[coin], reason="MANUAL")
        return {"closed": [coin]}

    # ------------------------------------------------------------------ #
    # Exit checks                                                          #
    # ------------------------------------------------------------------ #

    def _check_take_profits(self, positions: Dict[str, dict],
                            prices: Dict[str, float]) -> None:
        for coin, pos in list(positions.items()):
            entry = float(pos.get("entryPrice") or 0)
            if entry == 0:
                continue
            current = prices.get(coin, 0)
            if not current:
                continue
            side = pos["side"]
            gain = ((current - entry) / entry if side == "long"
                    else (entry - current) / entry)
            if gain >= TAKE_PROFIT_PCT:
                logger.info(f"TAKE PROFIT {coin} +{gain*100:.3f}%")
                self._exit(coin, pos, reason="TAKE PROFIT")
                positions.pop(coin, None)

    def _check_emergency_stops(self, positions: Dict[str, dict],
                                prices: Dict[str, float]) -> None:
        """Hard emergency exit at -3% — last resort, no regular SL."""
        for coin, pos in list(positions.items()):
            entry = float(pos.get("entryPrice") or 0)
            if entry == 0:
                continue
            current = prices.get(coin, 0)
            if not current:
                continue
            side = pos["side"]
            loss = ((entry - current) / entry if side == "long"
                    else (current - entry) / entry)
            if loss >= EMERGENCY_STOP_PCT:
                logger.warning(
                    f"EMERGENCY STOP {coin} {side.upper()} -{loss*100:.2f}% "
                    f"(entry=${entry:.4f} now=${current:.4f})"
                )
                self._exit(coin, pos, reason="EMERGENCY STOP")
                positions.pop(coin, None)

    # ------------------------------------------------------------------ #
    # News (dashboard only — not used for trade entries)                  #
    # ------------------------------------------------------------------ #

    def _refresh_news(self) -> None:
        if time.time() - self._last_news_refresh < NEWS_REFRESH_SECS:
            return
        try:
            news = get_news_signals()
            self._news_signals = news
            self._last_news_refresh = time.time()
            fg_val, fg_label = get_fear_greed()
            bot_state.update(news_signals=self._news_signals,
                             fear_greed=fg_val, fear_greed_label=fg_label)
        except Exception as e:
            logger.debug(f"news refresh: {e}")

    # ------------------------------------------------------------------ #
    # Main tick                                                            #
    # ------------------------------------------------------------------ #

    def _tick(self) -> None:
        equity = self._equity()
        bot_state.update(equity=equity, last_tick=time.time(),
                         active_strategy=active_strategy())

        if self._check_kill_switch(equity):
            pct = (self._day_open_equity - equity) / self._day_open_equity * 100
            logger.warning(f"KILL SWITCH — equity down {pct:.2f}%. Halting.")
            for coin, pos in self._all_positions().items():
                self._exit(coin, pos, reason="KILL SWITCH")
            self._halted = True
            bot_state.update(halted=True)
            return

        self._refresh_news()

        # ---- Fetch prices and open positions ----
        prices = {}
        try:
            syms = [_sym(c) for c in SCAN_COINS if _sym(c) in self.exchange.markets]
            tickers = self.exchange.fetch_tickers(syms)
            prices = {
                sym.split("/")[0]: float(t["last"])
                for sym, t in tickers.items() if t.get("last")
            }
        except Exception as e:
            logger.warning(f"fetch_tickers: {e}")

        open_positions = self._all_positions()

        bot_state.update(positions=[
            {"coin": c, "side": p["side"],
             "size": abs(float(p["contracts"])),
             "entry_px": float(p.get("entryPrice") or 0),
             "upnl": float(p.get("unrealizedPnl") or 0)}
            for c, p in open_positions.items()
        ])

        # ---- Manage existing position ----
        if open_positions:
            self._check_take_profits(open_positions, prices)
            self._check_emergency_stops(open_positions, prices)
            return  # one trade at a time — wait for exit before new entry

        # ---- Look for the next high-conviction trade ----
        setup = self._best_coin()
        if setup is None:
            logger.debug("No qualifying setup found — waiting")
            return

        coin, signal, score = setup
        price = prices.get(coin)
        if not price:
            return

        since_last = time.time() - self._last_trade_time.get(coin, 0)
        if since_last < TRADE_COOLDOWN_SECS:
            return

        logger.info(
            f"SETUP [{score:.1f}/4] {coin}: {signal} @ ${price:.4f} — entering"
        )
        bot_state.update(signal=f"{coin}:{signal}")
        try:
            self._enter(coin, signal, price)
        except Exception as e:
            logger.error(f"Entry error {coin}: {e}")

    def _check_kill_switch(self, equity: float) -> bool:
        if self._day_open_equity is None:
            return False
        return (self._day_open_equity - equity) / self._day_open_equity >= KILL_SWITCH_DRAWDOWN

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        logger.info(
            "Momentum Scalper starting — 1 trade at a time, "
            f"${POSITION_SIZE_USD:.0f} notional, TP +{TAKE_PROFIT_PCT*100:.2f}%, no SL"
        )
        self._day_open_equity = self._equity()
        bot_state.update(day_open_equity=self._day_open_equity,
                         equity=self._day_open_equity)
        logger.info(f"Day-open equity: ${self._day_open_equity:.2f}")

        while True:
            if self._halted:
                logger.warning("Bot halted. Exiting.")
                return
            try:
                self._tick()
            except Exception as exc:
                logger.error(f"Tick error: {exc}", exc_info=True)
            time.sleep(LOOP_INTERVAL_SECS)
