import logging
import os
import time
from typing import Dict, Optional

import ccxt
from dotenv import load_dotenv

from news import get_news_signals
from state import bot_state
from strategy import get_signal

load_dotenv()

logger = logging.getLogger(__name__)

POSITION_SIZE_USD = 10.0
KILL_SWITCH_DRAWDOWN = 0.05
LOOP_INTERVAL_SECS = 10
RSI_CANDLE_COUNT = 60
TRADE_COOLDOWN_SECS = 60
NEWS_REFRESH_SECS = 60
MAX_POSITIONS = 3           # max concurrent open positions


def _sym(coin: str) -> str:
    return f"{coin}/USD:USD"


class TradingBot:
    def __init__(self) -> None:
        self.exchange = ccxt.krakenfutures({
            "apiKey": os.environ["KRAKEN_API_KEY"],
            "secret": os.environ["KRAKEN_API_SECRET"],
        })
        self.exchange.set_sandbox_mode(True)
        self.exchange.load_markets()

        self._news_api_key: str = ""  # no longer needed — using CoinGecko free API
        self._day_open_equity: Optional[float] = None
        self._halted = False
        self._last_trade_time: Dict[str, float] = {}
        self._last_news_refresh: float = 0.0
        self._news_signals: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Data helpers                                                         #
    # ------------------------------------------------------------------ #

    def _equity(self) -> float:
        balance = self.exchange.fetch_balance()
        return float(balance["USD"]["total"])

    def _price(self, coin: str) -> float:
        return float(self.exchange.fetch_ticker(_sym(coin))["last"])

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

    def _closes(self, coin: str) -> list:
        ohlcv = self.exchange.fetch_ohlcv(_sym(coin), "1m", limit=RSI_CANDLE_COUNT)
        return [c[4] for c in ohlcv]

    # ------------------------------------------------------------------ #
    # Order helpers                                                        #
    # ------------------------------------------------------------------ #

    def _enter(self, coin: str, signal: str, price: float) -> None:
        sym = _sym(coin)
        is_buy = signal == "BUY"
        size = float(self.exchange.amount_to_precision(sym, POSITION_SIZE_USD / price))
        direction = "LONG" if is_buy else "SHORT"
        logger.info(f"ENTER {direction} {size} {sym} @ ~${price:.4f}")
        if is_buy:
            result = self.exchange.create_market_buy_order(sym, size)
        else:
            result = self.exchange.create_market_sell_order(sym, size)
        logger.info(f"Order id={result['id']} status={result['status']}")
        self._last_trade_time[coin] = time.time()
        bot_state.add_trade({
            "time": time.time(), "action": "ENTER", "coin": coin,
            "side": "long" if is_buy else "short",
            "size": size, "price": price, "order_id": result.get("id"),
        })

    def _exit(self, coin: str, pos: dict) -> None:
        sym = _sym(coin)
        size = abs(float(pos["contracts"]))
        side = pos["side"]
        logger.info(f"EXIT {side.upper()} {size} {sym}")
        params = {"reduceOnly": True}
        result = (self.exchange.create_market_sell_order(sym, size, params=params)
                  if side == "long" else
                  self.exchange.create_market_buy_order(sym, size, params=params))
        logger.info(f"Close id={result['id']} status={result['status']}")
        bot_state.add_trade({
            "time": time.time(), "action": "EXIT", "coin": coin,
            "side": side, "size": size,
            "price": float(self.exchange.fetch_ticker(sym)["last"]),
            "order_id": result.get("id"),
        })

    # ------------------------------------------------------------------ #
    # Core logic                                                           #
    # ------------------------------------------------------------------ #

    def _check_kill_switch(self, equity: float) -> bool:
        if self._day_open_equity is None:
            return False
        return (self._day_open_equity - equity) / self._day_open_equity >= KILL_SWITCH_DRAWDOWN

    def _refresh_news(self) -> None:
        if not self._news_api_key:
            return
        if time.time() - self._last_news_refresh < NEWS_REFRESH_SECS:
            return
        self._news_signals = get_news_signals(self._news_api_key)
        self._last_news_refresh = time.time()
        bot_state.update(news_signals=self._news_signals)

    def _tick(self) -> None:
        equity = self._equity()
        logger.info(f"Equity=${equity:.2f}")
        bot_state.update(equity=equity, last_tick=time.time())

        if self._check_kill_switch(equity):
            pct = (self._day_open_equity - equity) / self._day_open_equity * 100
            logger.warning(f"KILL SWITCH — equity down {pct:.2f}%. Halting.")
            for coin, pos in self._all_positions().items():
                self._exit(coin, pos)
            self._halted = True
            bot_state.update(halted=True)
            return

        self._refresh_news()

        if not self._news_signals:
            logger.info("No news signals. Waiting...")
            return

        open_positions = self._all_positions()

        # Update dashboard positions
        bot_state.update(positions=[
            {
                "coin": coin,
                "side": pos["side"],
                "size": abs(float(pos["contracts"])),
                "entry_px": float(pos.get("entryPrice") or 0),
                "upnl": float(pos.get("unrealizedPnl") or 0),
            }
            for coin, pos in open_positions.items()
        ])

        for coin, news_signal in self._news_signals.items():
            try:
                sym = _sym(coin)
                if sym not in self.exchange.markets:
                    continue

                pos = open_positions.get(coin)
                pos_side = pos["side"].upper() if pos else None

                # Exit if news flipped against current position
                if pos_side == "LONG" and news_signal == "SELL":
                    logger.info(f"{coin}: news flipped bearish — closing long")
                    self._exit(coin, pos)
                    open_positions.pop(coin, None)
                    continue
                if pos_side == "SHORT" and news_signal == "BUY":
                    logger.info(f"{coin}: news flipped bullish — closing short")
                    self._exit(coin, pos)
                    open_positions.pop(coin, None)
                    continue

                if pos:
                    continue  # already in position, hold it

                if len(open_positions) >= MAX_POSITIONS:
                    continue  # at max positions

                since_last = time.time() - self._last_trade_time.get(coin, 0)
                if since_last < TRADE_COOLDOWN_SECS:
                    continue

                price = self._price(coin)
                closes = self._closes(coin)
                if len(closes) < 23:
                    continue

                tech_signal = get_signal(closes)
                price_rising = closes[-1] > closes[-2]
                price_falling = closes[-1] < closes[-2]

                logger.info(f"{coin}: news={news_signal} tech={tech_signal} price=${price:.4f}")
                bot_state.update(signal=f"{coin}:{news_signal}")

                if news_signal == "BUY" and tech_signal == "BUY" and price_rising:
                    self._enter(coin, "BUY", price)
                    open_positions[coin] = True
                elif news_signal == "SELL" and tech_signal == "SELL" and price_falling:
                    self._enter(coin, "SELL", price)
                    open_positions[coin] = True

            except Exception as e:
                logger.error(f"Error on {coin}: {e}")

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        logger.info("Bot starting — Kraken Futures demo [CoinGecko news-driven multi-coin]")
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
