import logging
import os
import time
from typing import Optional

import ccxt
from dotenv import load_dotenv

from state import bot_state
from strategy import get_signal

load_dotenv()

logger = logging.getLogger(__name__)

SYMBOL = "SOL/USD:USD"          # Kraken Futures SOL perpetual
POSITION_SIZE_USD = 10.0
KILL_SWITCH_DRAWDOWN = 0.05     # halt if equity falls 5% from day-open value
LOOP_INTERVAL_SECS = 10
RSI_CANDLE_COUNT = 60           # fetch 60 1-minute candles; need ≥15 for RSI-14
TRADE_COOLDOWN_SECS = 60        # minimum seconds between trades to avoid overtrading


class TradingBot:
    def __init__(self) -> None:
        self.exchange = ccxt.krakenfutures({
            "apiKey": os.environ["KRAKEN_API_KEY"],
            "secret": os.environ["KRAKEN_API_SECRET"],
        })
        self.exchange.set_sandbox_mode(True)  # demo-futures.kraken.com
        self.exchange.load_markets()

        self._day_open_equity: Optional[float] = None
        self._halted = False
        self._last_trade_time: float = 0.0

    # ------------------------------------------------------------------ #
    # Data helpers                                                         #
    # ------------------------------------------------------------------ #

    def _equity(self) -> float:
        balance = self.exchange.fetch_balance()
        return float(balance["USD"]["total"])

    def _mid_price(self) -> float:
        ticker = self.exchange.fetch_ticker(SYMBOL)
        return float(ticker["last"])

    def _open_position(self) -> Optional[dict]:
        """Return the current SOL perp position dict, or None if flat."""
        positions = self.exchange.fetch_positions([SYMBOL])
        for pos in positions:
            if pos.get("contracts") and float(pos["contracts"]) != 0:
                return pos
        return None

    def _fetch_closes(self) -> list:
        ohlcv = self.exchange.fetch_ohlcv(SYMBOL, "1m", limit=RSI_CANDLE_COUNT)
        return [candle[4] for candle in ohlcv]  # [time, open, high, low, CLOSE, vol]

    # ------------------------------------------------------------------ #
    # Order helpers                                                        #
    # ------------------------------------------------------------------ #

    def _enter(self, signal: str, price: float) -> None:
        is_buy = signal == "BUY"
        size = float(self.exchange.amount_to_precision(SYMBOL, POSITION_SIZE_USD / price))
        direction = "LONG" if is_buy else "SHORT"
        logger.info(f"ENTER {direction} {size} contracts {SYMBOL} @ ~${price:.4f}")
        if is_buy:
            result = self.exchange.create_market_buy_order(SYMBOL, size)
        else:
            result = self.exchange.create_market_sell_order(SYMBOL, size)
        logger.info(f"Order id={result['id']} status={result['status']}")
        self._last_trade_time = time.time()
        bot_state.add_trade({
            "time": self._last_trade_time,
            "action": "ENTER",
            "side": "long" if is_buy else "short",
            "size": size,
            "price": price,
            "order_id": result.get("id"),
        })

    def _exit(self, pos: dict) -> None:
        size = abs(float(pos["contracts"]))
        side = pos["side"]  # 'long' or 'short'
        logger.info(f"EXIT {side.upper()} {size} contracts {SYMBOL}")
        params = {"reduceOnly": True}
        if side == "long":
            result = self.exchange.create_market_sell_order(SYMBOL, size, params=params)
        else:
            result = self.exchange.create_market_buy_order(SYMBOL, size, params=params)
        logger.info(f"Close id={result['id']} status={result['status']}")
        bot_state.add_trade({
            "time": time.time(),
            "action": "EXIT",
            "side": side,
            "size": size,
            "price": bot_state.price,
            "order_id": result.get("id"),
        })

    # ------------------------------------------------------------------ #
    # Core logic                                                           #
    # ------------------------------------------------------------------ #

    def _check_kill_switch(self, equity: float) -> bool:
        if self._day_open_equity is None:
            return False
        drawdown = (self._day_open_equity - equity) / self._day_open_equity
        return drawdown >= KILL_SWITCH_DRAWDOWN

    def _tick(self) -> None:
        equity = self._equity()
        price = self._mid_price()
        logger.info(f"Equity=${equity:.2f} | {SYMBOL} price=${price:.4f}")

        bot_state.update(equity=equity, price=price, last_tick=time.time())

        if self._check_kill_switch(equity):
            pct = (self._day_open_equity - equity) / self._day_open_equity * 100
            logger.warning(
                f"KILL SWITCH — equity down {pct:.2f}% "
                f"(day-open ${self._day_open_equity:.2f} → ${equity:.2f}). Halting."
            )
            pos = self._open_position()
            if pos:
                self._exit(pos)
            self._halted = True
            bot_state.update(halted=True)
            return

        closes = self._fetch_closes()
        if len(closes) < 16:
            logger.warning(f"Insufficient candle data ({len(closes)} bars). Skipping tick.")
            return

        signal = get_signal(closes)
        pos = self._open_position()
        pos_side = pos["side"].upper() if pos else None  # 'LONG' or 'SHORT'

        bot_state.update(
            signal=signal,
            position_side=pos["side"] if pos else None,
            position_size=abs(float(pos["contracts"])) if pos else 0.0,
            position_entry_px=float(pos.get("entryPrice") or 0) if pos else 0.0,
            unrealized_pnl=float(pos.get("unrealizedPnl") or 0) if pos else 0.0,
        )

        cooldown_remaining = TRADE_COOLDOWN_SECS - (time.time() - self._last_trade_time)
        in_cooldown = cooldown_remaining > 0
        logger.info(f"Signal={signal} | Position={pos_side or 'FLAT'}" +
                    (f" | Cooldown={cooldown_remaining:.0f}s" if in_cooldown else ""))

        # Only trade if price is moving in the signal direction (momentum confirmation)
        price_rising = closes[-1] > closes[-2]
        price_falling = closes[-1] < closes[-2]

        if signal == "BUY" and price_rising:
            if pos_side == "SHORT":
                self._exit(pos)
                self._enter("BUY", price)
            elif pos_side is None and not in_cooldown:
                self._enter("BUY", price)

        elif signal == "SELL" and price_falling:
            if pos_side == "LONG":
                self._exit(pos)
                self._enter("SELL", price)
            elif pos_side is None and not in_cooldown:
                self._enter("SELL", price)

        # HOLD → no action

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        logger.info("Bot starting — Kraken Futures demo")
        self._day_open_equity = self._equity()
        bot_state.update(day_open_equity=self._day_open_equity, equity=self._day_open_equity)
        logger.info(f"Day-open equity: ${self._day_open_equity:.2f}")

        while True:
            if self._halted:
                logger.warning("Bot halted by kill switch. Exiting.")
                return

            try:
                self._tick()
            except Exception as exc:
                logger.error(f"Tick error: {exc}", exc_info=True)

            time.sleep(LOOP_INTERVAL_SECS)
