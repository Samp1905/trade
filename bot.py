import logging
import os
import time
from typing import Dict, List, Optional

import ccxt
from dotenv import load_dotenv

from news import get_news_signals, get_fear_greed, COIN_MAP
from state import bot_state
from strategy import get_signal, active_strategy, _ema, _rsi, _sma, _vwap

load_dotenv()

logger = logging.getLogger(__name__)

POSITION_SIZE_USD    = 2000.0
KILL_SWITCH_DRAWDOWN = 0.05
LOOP_INTERVAL_SECS   = 10       # tick every 10s
TRADE_COOLDOWN_SECS  = 60       # minimum 60s between trades per coin
TAKE_PROFIT_PCT      = 0.004    # exit at 0.4% gain (~$8 on $2000)
STOP_LOSS_PCT        = 0.002    # exit at 0.2% loss (~$4 on $2000) — 2:1 R/R
MAX_POSITIONS        = 5        # max 5 concurrent positions
SIGNAL_REFRESH_SECS  = 15       # refresh 1m OHLCV every 15s
NEWS_REFRESH_SECS    = 60       # CoinGecko poll (rate-limit friendly)
EQUITY_REFRESH_SECS  = 30
FIVE_MIN_MOVE_PCT    = 0.10

# SOL is always watched; news/5m movers add more coins dynamically
DEFAULT_WATCHLIST = {"SOL"}


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
        # Retry load_markets — Kraken demo occasionally returns 503 on startup
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

        # Shared OHLCV cache — one fetch per coin per SIGNAL_REFRESH_SECS
        self._ohlcv_cache: Dict[str, List] = {}
        self._last_ohlcv_time: Dict[str, float] = {}
        self._ohlcv_5m_cache: Dict[str, List] = {}
        self._last_ohlcv_5m_time: Dict[str, float] = {}

        # SMA200+RSI10 strategy state
        self._entry_swing_lows: Dict[str, float] = {}   # coin → swing low at entry
        self._prev_rsi10: Dict[str, float] = {}          # coin → RSI(10) last tick

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

    def _batch_prices(self, coins: list) -> Dict[str, float]:
        syms = [_sym(c) for c in coins if _sym(c) in self.exchange.markets]
        if not syms:
            return {}
        tickers = self.exchange.fetch_tickers(syms)
        return {
            sym.split("/")[0]: float(t["last"])
            for sym, t in tickers.items()
            if t.get("last")
        }

    def _get_ohlcv(self, coin: str) -> List:
        """Cached 1m OHLCV; refreshed every SIGNAL_REFRESH_SECS."""
        now = time.time()
        if now - self._last_ohlcv_time.get(coin, 0) > SIGNAL_REFRESH_SECS:
            try:
                self._ohlcv_cache[coin] = self.exchange.fetch_ohlcv(
                    _sym(coin), "1m", limit=210    # 210 needed for SMA 200
                )
                self._last_ohlcv_time[coin] = now
            except Exception as e:
                logger.debug(f"OHLCV 1m {coin}: {e}")
        return self._ohlcv_cache.get(coin, [])

    def _get_ohlcv_5m(self, coin: str) -> List:
        """Cached 5m OHLCV for multi-timeframe confirmation; refreshed every 60s."""
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

    def _strategy_signal(self, coin: str) -> str:
        """Strategy signal from the auto-selector with full OHLCV + 5m MTF."""
        ohlcv    = self._get_ohlcv(coin)
        ohlcv_5m = self._get_ohlcv_5m(coin)
        if not ohlcv:
            return "HOLD"
        try:
            sig = get_signal(ohlcv, ohlcv_5m)
            bot_state.update(active_strategy=active_strategy())
            return sig
        except Exception as e:
            logger.debug(f"Strategy signal {coin}: {e}")
            return "HOLD"

    def _htf_trend_gate(self, coin: str, signal: str) -> bool:
        """
        Allow a signal only when the 5m EMA9/EMA21 trend agrees.
        Prevents buying into a downtrend and selling into an uptrend.
        """
        ohlcv_5m = self._get_ohlcv_5m(coin)
        if len(ohlcv_5m) < 25:
            return True     # not enough data — don't block
        closes_5m = [c[4] for c in ohlcv_5m]
        e9  = _ema(closes_5m, 9)
        e21 = _ema(closes_5m, 21)
        if signal == "BUY"  and e9 > e21:
            return True
        if signal == "SELL" and e9 < e21:
            return True
        return False

    def _recent_swing_low(self, coin: str, entry_price: float) -> float:
        """Most recent local low in last 20 candles — used as stop loss level."""
        ohlcv = self._get_ohlcv(coin)
        if not ohlcv:
            return entry_price * 0.995
        lows = [c[3] for c in ohlcv[-20:]]
        for i in range(len(lows) - 2, 0, -1):
            if lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
                sl = lows[i] * 0.9995      # tiny buffer below the swing
                return sl if sl < entry_price else min(lows) * 0.9995
        return min(lows) * 0.9995

    def _check_rsi_exits(self, positions: Dict[str, dict]) -> None:
        """Exit long positions when RSI(10) crosses above 40."""
        for coin, pos in list(positions.items()):
            if pos.get("side") != "long":
                continue
            ohlcv = self._get_ohlcv(coin)
            if not ohlcv or len(ohlcv) < 12:
                continue
            closes   = [c[4] for c in ohlcv]
            curr_rsi = _rsi(closes, 10)
            prev_rsi = self._prev_rsi10.get(coin, curr_rsi)
            self._prev_rsi10[coin] = curr_rsi
            if prev_rsi <= 40 and curr_rsi > 40:
                logger.info(f"RSI(10) EXIT {coin}: {prev_rsi:.1f}→{curr_rsi:.1f} crossed 40")
                self._exit(coin, pos, reason="RSI EXIT")
                positions.pop(coin, None)

    def _check_swing_low_stops(self, positions: Dict[str, dict],
                                prices: Dict[str, float]) -> None:
        """Exit long positions that close below the entry swing low."""
        for coin, pos in list(positions.items()):
            if pos.get("side") != "long":
                continue
            sl = self._entry_swing_lows.get(coin)
            if sl is None:
                continue
            current = prices.get(coin, 0)
            if current and current < sl:
                logger.warning(f"SWING LOW STOP {coin}: ${current:.4f} < swing low ${sl:.4f}")
                self._exit(coin, pos, reason="SWING LOW STOP")
                positions.pop(coin, None)

    def _news_chart_signal(self, coin: str) -> Optional[str]:
        """
        News-driven strategy: headline/CoinGecko signal confirmed by chart.
        News gives the directional bias; VWAP + EMA + RSI must agree (2-of-3)
        before the trade fires. Prevents chasing stale news on exhausted moves.
        """
        news_sig = self._news_signals.get(coin)
        if not news_sig:
            return None
        ohlcv = self._get_ohlcv(coin)
        if not ohlcv or len(ohlcv) < 25:
            return news_sig             # no chart data yet — trust news alone
        closes = [c[4] for c in ohlcv]
        price  = closes[-1]
        vwap   = _vwap(ohlcv)
        e9     = _ema(closes, 9)
        e21    = _ema(closes, 21)
        rsi14  = _rsi(closes, 14)

        if news_sig == "BUY":
            bull_count = (price > vwap) + (e9 > e21) + (rsi14 > 50)
            return "BUY" if bull_count >= 2 else None
        else:  # SELL
            bear_count = (price < vwap) + (e9 < e21) + (rsi14 < 50)
            return "SELL" if bear_count >= 2 else None

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
    # Order helpers                                                        #
    # ------------------------------------------------------------------ #

    def _place_order(self, sym: str, side: str, size: float,
                     ask: float, bid: float, params: dict = None) -> dict:
        """Limit GTC at the current ask (buy) or bid (sell)."""
        is_buy = side == "buy"
        lp = float(self.exchange.price_to_precision(sym, ask if is_buy else bid))
        p = {**(params or {})}
        logger.debug(f"order {side} {sym} size={size} lp={lp:.4f} (ask={ask:.4f} bid={bid:.4f})")
        return (self.exchange.create_limit_buy_order(sym, size, lp, params=p)
                if is_buy else
                self.exchange.create_limit_sell_order(sym, size, lp, params=p))

    def _fetch_bid_ask(self, sym: str) -> tuple:
        t = self.exchange.fetch_ticker(sym)
        last = float(t["last"] or 0)
        ask = float(t["ask"] or last)
        bid = float(t["bid"] or last)
        return last, ask, bid

    def _cancel_open_orders(self, coin: str) -> None:
        """Cancel any resting orders for coin to prevent selfFill on exit."""
        sym = _sym(coin)
        try:
            for o in self.exchange.fetch_open_orders(sym):
                try:
                    self.exchange.cancel_order(o["id"], sym)
                    logger.debug(f"Cancelled resting order {o['id']} for {coin}")
                except Exception as e:
                    logger.debug(f"Cancel {o['id']}: {e}")
        except Exception as e:
            logger.debug(f"fetch_open_orders {coin}: {e}")

    def _enter(self, coin: str, signal: str, price: float) -> None:
        sym = _sym(coin)
        is_buy = signal == "BUY"
        size = float(self.exchange.amount_to_precision(sym, POSITION_SIZE_USD / price))
        self._cancel_open_orders(coin)
        last, ask, bid = self._fetch_bid_ask(sym)
        if is_buy:
            self._entry_swing_lows[coin] = self._recent_swing_low(coin, last)
            logger.info(f"ENTER LONG {size} {sym} @ ~${last:.4f}  SL swing-low=${self._entry_swing_lows[coin]:.4f}")
        else:
            logger.info(f"ENTER SHORT {size} {sym} @ ~${last:.4f}")
        result = self._place_order(sym, "buy" if is_buy else "sell", size, ask, bid)
        self._last_trade_time[coin] = time.time()
        bot_state.add_trade({
            "time": time.time(), "action": "ENTER", "coin": coin,
            "side": "long" if is_buy else "short",
            "size": size, "price": last, "order_id": result.get("id"),
        })
        logger.info(f"Order {result.get('id')} {result.get('status')}")

    def _exit(self, coin: str, pos: dict, reason: str = "EXIT") -> None:
        sym = _sym(coin)
        size = abs(float(pos["contracts"]))
        side = pos["side"]
        self._cancel_open_orders(coin)
        last, ask, bid = self._fetch_bid_ask(sym)
        exit_side = "sell" if side == "long" else "buy"
        result = self._place_order(sym, exit_side, size, ask, bid, {"reduceOnly": True})
        logger.info(f"{reason} {side.upper()} {size} {sym} @ ~${last:.4f}")
        bot_state.add_trade({
            "time": time.time(), "action": reason, "coin": coin,
            "side": side, "size": size, "price": last,
            "order_id": result.get("id"),
        })

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
    # Risk checks                                                          #
    # ------------------------------------------------------------------ #

    def _check_take_profits(self, positions: Dict[str, dict],
                             prices: Dict[str, float]) -> None:
        for coin, pos in list(positions.items()):
            entry = float(pos.get("entryPrice") or 0)
            if entry == 0:
                continue
            current = prices.get(coin, 0)
            if current == 0:
                continue
            side = pos["side"]
            gain_pct = ((current - entry) / entry if side == "long"
                        else (entry - current) / entry)
            if gain_pct >= TAKE_PROFIT_PCT:
                logger.info(f"TAKE PROFIT {coin} +{gain_pct*100:.3f}%")
                self._exit(coin, pos, reason="TAKE PROFIT")
                positions.pop(coin, None)

    def _check_stop_losses(self, positions: Dict[str, dict],
                            prices: Dict[str, float]) -> None:
        for coin, pos in list(positions.items()):
            entry = float(pos.get("entryPrice") or 0)
            if entry == 0:
                continue
            current = prices.get(coin, 0)
            if current == 0:
                continue
            side = pos["side"]
            loss_pct = ((entry - current) / entry if side == "long"
                        else (current - entry) / entry)
            if loss_pct >= STOP_LOSS_PCT:
                logger.warning(
                    f"STOP LOSS {coin} {side.upper()} "
                    f"entry=${entry:.4f} now=${current:.4f} -{loss_pct*100:.1f}%"
                )
                self._exit(coin, pos, reason="STOP LOSS")
                positions.pop(coin, None)

    # ------------------------------------------------------------------ #
    # Watchlist / news                                                     #
    # ------------------------------------------------------------------ #

    def _refresh_news(self) -> None:
        if time.time() - self._last_news_refresh < NEWS_REFRESH_SECS:
            return
        news = get_news_signals()
        movers = self._scan_5m_movers()
        self._news_signals = {**news, **movers}
        self._last_news_refresh = time.time()
        fg_val, fg_label = get_fear_greed()
        bot_state.update(news_signals=self._news_signals,
                         fear_greed=fg_val, fear_greed_label=fg_label)

    def _scan_5m_movers(self) -> Dict[str, str]:
        signals = {}
        for coin in COIN_MAP.values():
            try:
                sym = _sym(coin)
                if sym not in self.exchange.markets:
                    continue
                ohlcv = self.exchange.fetch_ohlcv(sym, "5m", limit=2)
                if len(ohlcv) < 2:
                    continue
                o, c = ohlcv[-2][1], ohlcv[-2][4]
                if o == 0:
                    continue
                move = (c - o) / o
                if move >= FIVE_MIN_MOVE_PCT:
                    signals[coin] = "BUY"
                elif move <= -FIVE_MIN_MOVE_PCT:
                    signals[coin] = "SELL"
            except Exception as e:
                logger.debug(f"5m scan {coin}: {e}")
        return signals

    # ------------------------------------------------------------------ #
    # Main tick                                                            #
    # ------------------------------------------------------------------ #

    def _tick(self) -> None:
        equity = self._equity()
        bot_state.update(equity=equity, last_tick=time.time())

        if self._check_kill_switch(equity):
            pct = (self._day_open_equity - equity) / self._day_open_equity * 100
            logger.warning(f"KILL SWITCH — equity down {pct:.2f}%. Halting.")
            for coin, pos in self._all_positions().items():
                self._exit(coin, pos, reason="KILL SWITCH")
            self._halted = True
            bot_state.update(halted=True)
            return

        self._refresh_news()

        watchlist = DEFAULT_WATCHLIST | set(self._news_signals.keys())
        watchlist = {c for c in watchlist if _sym(c) in self.exchange.markets}

        prices = self._batch_prices(list(watchlist))
        open_positions = self._all_positions()

        bot_state.update(positions=[
            {"coin": c, "side": p["side"],
             "size": abs(float(p["contracts"])),
             "entry_px": float(p.get("entryPrice") or 0),
             "upnl": float(p.get("unrealizedPnl") or 0)}
            for c, p in open_positions.items()
        ])

        self._check_take_profits(open_positions, prices)
        self._check_stop_losses(open_positions, prices)
        self._check_rsi_exits(open_positions)
        self._check_swing_low_stops(open_positions, prices)

        for coin in watchlist:
            try:
                price = prices.get(coin)
                if not price:
                    continue
                if open_positions.get(coin):
                    continue
                if len(open_positions) >= MAX_POSITIONS:
                    break
                since_last = time.time() - self._last_trade_time.get(coin, 0)
                if since_last < TRADE_COOLDOWN_SECS:
                    continue

                # Signal priority: news+chart > technical strategy
                news  = self._news_chart_signal(coin)
                strat = self._strategy_signal(coin)

                if news is not None:
                    signal = news
                elif strat != "HOLD":
                    signal = strat
                else:
                    continue

                # 5m trend gate — only trade with the higher-timeframe trend
                if not self._htf_trend_gate(coin, signal):
                    logger.debug(f"{coin}: {signal} blocked by 5m trend gate")
                    continue

                source = "NEWS" if news else "STRAT"

                logger.info(
                    f"{coin}: [{source}] signal={signal} strategy={active_strategy()} "
                    f"price=${price:.4f}"
                )
                bot_state.update(signal=f"{coin}:{signal}")
                self._enter(coin, signal, price)
                open_positions[coin] = True

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
        logger.info("Bot starting — Kraken Futures scalper (10s loop, $0.02 TP, 10% SL)")
        self._day_open_equity = self._equity()
        bot_state.update(day_open_equity=self._day_open_equity, equity=self._day_open_equity)
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
