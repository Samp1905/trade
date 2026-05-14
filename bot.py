import logging
import os
import time
from typing import Dict, Optional

import ccxt
from dotenv import load_dotenv

from news import get_news_signals, COIN_MAP
from state import bot_state
from strategy import get_signal, active_strategy

load_dotenv()

logger = logging.getLogger(__name__)

POSITION_SIZE_USD   = 10.0
KILL_SWITCH_DRAWDOWN = 0.05
LOOP_INTERVAL_SECS  = 30        # check every 30s — aligns with 1m candle rhythm
TRADE_COOLDOWN_SECS = 30        # allow re-entry 30s after last trade
TAKE_PROFIT_USD     = 0.02      # exit at $0.02 profit
STOP_LOSS_PCT       = 0.10      # exit at 10% loss
MAX_POSITIONS       = 3
SIGNAL_REFRESH_SECS = 60        # re-evaluate strategy on 1m candles every 60s
EQUITY_REFRESH_SECS = 30
FIVE_MIN_MOVE_PCT   = 0.10
SLIPPAGE_PCT        = 0.003     # 0.3% limit-IOC slippage to stay inside price collar

# SOL is always watched; news/5m movers add more coins dynamically
DEFAULT_WATCHLIST  = {"SOL"}


def _sym(coin: str) -> str:
    return f"{coin}/USD:USD"


class TradingBot:
    def __init__(self) -> None:
        self.exchange = ccxt.krakenfutures({
            "apiKey": os.environ["KRAKEN_API_KEY"],
            "secret": os.environ["KRAKEN_API_SECRET"],
            "timeout": 30000,  # 30s timeout (default is 10s)
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
        self._prev_prices: Dict[str, float] = {}    # for tick momentum
        self._strat_signals: Dict[str, str] = {}    # cached strategy signals per coin
        self._last_strat_time: Dict[str, float] = {}
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
                pass  # return stale cache on timeout
        return self._cached_equity

    def _batch_prices(self, coins: list) -> Dict[str, float]:
        """Fetch all coin prices in one API call."""
        syms = [_sym(c) for c in coins if _sym(c) in self.exchange.markets]
        if not syms:
            return {}
        tickers = self.exchange.fetch_tickers(syms)
        return {
            sym.split("/")[0]: float(t["last"])
            for sym, t in tickers.items()
            if t.get("last")
        }

    def _strategy_signal(self, coin: str) -> str:
        """Return cached strategy signal for coin; refreshes every 60s."""
        now = time.time()
        if now - self._last_strat_time.get(coin, 0) > SIGNAL_REFRESH_SECS:
            try:
                ohlcv = self.exchange.fetch_ohlcv(_sym(coin), "1m", limit=100)
                closes = [c[4] for c in ohlcv]
                self._strat_signals[coin] = get_signal(closes)
                self._last_strat_time[coin] = now
                bot_state.update(active_strategy=active_strategy())
            except Exception as e:
                logger.debug(f"Strategy refresh {coin}: {e}")
        return self._strat_signals.get(coin, "HOLD")

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

    def _limit_ioc(self, sym: str, side: str, size: float,
                   ask: float, bid: float, params: dict = None) -> dict:
        """Limit IOC using live ask/bid so the order crosses the spread immediately."""
        is_buy = side == "buy"
        # Buy above ask, sell below bid — guarantees crossing the spread
        ref = ask if is_buy else bid
        lp = ref * (1 + SLIPPAGE_PCT) if is_buy else ref * (1 - SLIPPAGE_PCT)
        lp = float(self.exchange.price_to_precision(sym, lp))
        p = {"timeInForce": "ioc", **(params or {})}
        logger.debug(f"limit-IOC {side} {sym} size={size} lp={lp:.4f} (ask={ask:.4f} bid={bid:.4f})")
        return (self.exchange.create_limit_buy_order(sym, size, lp, params=p)
                if is_buy else
                self.exchange.create_limit_sell_order(sym, size, lp, params=p))

    def _fetch_bid_ask(self, sym: str) -> tuple:
        """Returns (last, ask, bid) from a fresh ticker."""
        t = self.exchange.fetch_ticker(sym)
        last = float(t["last"] or 0)
        ask = float(t["ask"] or last)
        bid = float(t["bid"] or last)
        return last, ask, bid

    def _enter(self, coin: str, signal: str, price: float) -> None:
        sym = _sym(coin)
        is_buy = signal == "BUY"
        size = float(self.exchange.amount_to_precision(sym, POSITION_SIZE_USD / price))
        last, ask, bid = self._fetch_bid_ask(sym)
        logger.info(f"ENTER {'LONG' if is_buy else 'SHORT'} {size} {sym} @ ~${last:.4f}")
        result = self._limit_ioc(sym, "buy" if is_buy else "sell", size, ask, bid)
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
        last, ask, bid = self._fetch_bid_ask(sym)
        exit_side = "sell" if side == "long" else "buy"
        result = self._limit_ioc(sym, exit_side, size, ask, bid, {"reduceOnly": True})
        logger.info(f"{reason} {side.upper()} {size} {sym} @ ~${last:.4f}")
        bot_state.add_trade({
            "time": time.time(), "action": reason, "coin": coin,
            "side": side, "size": size, "price": last,
            "order_id": result.get("id"),
        })

    def _close_by_coin(self, coin: str) -> dict:
        """Called from dashboard manual-close button."""
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
    # Risk checks (run every tick)                                         #
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
            size = abs(float(pos["contracts"]))
            side = pos["side"]
            upnl = ((current - entry) if side == "long" else (entry - current)) * size
            if upnl >= TAKE_PROFIT_USD:
                logger.info(f"TAKE PROFIT {coin} +${upnl:.4f}")
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
    # Watchlist / signals                                                  #
    # ------------------------------------------------------------------ #

    def _refresh_news(self) -> None:
        if time.time() - self._last_news_refresh < SIGNAL_REFRESH_SECS:
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

    def _tick_signal(self, coin: str, price: float) -> Optional[str]:
        """BUY if price ticked up, SELL if down, None if unchanged."""
        prev = self._prev_prices.get(coin)
        self._prev_prices[coin] = price
        if prev is None or price == prev:
            return None
        return "BUY" if price > prev else "SELL"

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

        # Build watchlist: default + news/5m movers
        watchlist = DEFAULT_WATCHLIST | set(self._news_signals.keys())
        watchlist = {c for c in watchlist if _sym(c) in self.exchange.markets}

        # Fetch all prices in one call
        prices = self._batch_prices(list(watchlist))
        open_positions = self._all_positions()

        # Update dashboard
        bot_state.update(positions=[
            {"coin": c, "side": p["side"],
             "size": abs(float(p["contracts"])),
             "entry_px": float(p.get("entryPrice") or 0),
             "upnl": float(p.get("unrealizedPnl") or 0)}
            for c, p in open_positions.items()
        ])

        # Risk checks first
        self._check_take_profits(open_positions, prices)
        self._check_stop_losses(open_positions, prices)

        # Entry logic — 1m candle strategy signal is the sole trigger
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

                # Strategy signal from 1m candles — this alone fires the trade
                signal = self._strategy_signal(coin)
                if signal == "HOLD":
                    continue

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
        logger.info("Bot starting — Kraken Futures scalper (2s loop, $0.02 TP, 10% SL)")
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
