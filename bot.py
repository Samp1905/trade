import logging
import os
import time
from typing import Dict, List, Optional

import ccxt
from dotenv import load_dotenv

from news import get_news_signals, COIN_MAP
from state import bot_state
from strategy import get_signal, active_strategy

load_dotenv()

logger = logging.getLogger(__name__)

POSITION_SIZE_USD    = 2000.0
KILL_SWITCH_DRAWDOWN = 0.05
LOOP_INTERVAL_SECS   = 10       # tick every 10s — catches every candle move
TRADE_COOLDOWN_SECS  = 10       # re-entry allowed 10s after last trade
TAKE_PROFIT_PCT      = 0.0015   # exit at 0.15% price gain (~$3 on $2000)
STOP_LOSS_PCT        = 0.001    # exit at 0.10% price loss (~$2 on $2000) — 1.5:1 R/R
MAX_POSITIONS        = 20
SIGNAL_REFRESH_SECS  = 15       # refresh 1m OHLCV every 15s
NEWS_REFRESH_SECS    = 60       # CoinGecko poll (rate-limit friendly)
EQUITY_REFRESH_SECS  = 30
FIVE_MIN_MOVE_PCT    = 0.10
CANDLE_BODY_PCT      = 0.0015   # 0.15% candle body — filters noise, confirms momentum

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
        self.exchange.load_markets()

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
                    _sym(coin), "1m", limit=100
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

    def _candle_signal(self, coin: str) -> Optional[str]:
        """
        Live candle-body momentum signal.
        Fires BUY/SELL when the current 1m candle has moved >= CANDLE_BODY_PCT
        from its open — catches moves as they develop, not just on completion.
        """
        ohlcv = self._get_ohlcv(coin)
        if len(ohlcv) < 1:
            return None
        candle = ohlcv[-1]          # in-progress candle
        o, c = candle[1], candle[4]
        if o == 0:
            return None
        move = (c - o) / o
        if move >= CANDLE_BODY_PCT:
            return "BUY"
        if move <= -CANDLE_BODY_PCT:
            return "SELL"
        return None

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
        logger.info(f"ENTER {'LONG' if is_buy else 'SHORT'} {size} {sym} @ ~${last:.4f}")
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
        bot_state.update(news_signals=self._news_signals)

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

                # Require strategy AND live candle to agree — filters noise
                strat  = self._strategy_signal(coin)
                candle = self._candle_signal(coin)

                if strat != "HOLD" and strat == candle:
                    signal = strat           # both agree — high confidence
                elif strat != "HOLD" and candle is None:
                    signal = strat           # candle neutral, trust strategy alone
                else:
                    continue                 # signals conflict or both HOLD

                logger.info(
                    f"{coin}: signal={signal} strategy={active_strategy()} "
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
