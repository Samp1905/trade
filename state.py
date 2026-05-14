from threading import Lock
from typing import Callable, Optional

KILL_SWITCH_DRAWDOWN = 0.05


class BotState:
    def __init__(self) -> None:
        self._lock = Lock()
        self.equity: float = 0.0
        self.day_open_equity: float = 0.0
        self.signal: str = "—"
        self.active_strategy: str = "—"
        self.positions: list = []       # list of {coin, side, size, entry_px, upnl}
        self.news_signals: dict = {}    # {coin: "BUY"/"SELL"}
        self.fear_greed: int = 50
        self.fear_greed_label: str = "Neutral"
        self.halted: bool = False
        self.last_tick: Optional[float] = None
        self.recent_trades: list = []
        self._close_fn: Optional[Callable[[str], dict]] = None

    def register_close(self, fn: Callable[[str], dict]) -> None:
        self._close_fn = fn

    def manual_close(self, coin: str) -> dict:
        fn = self._close_fn
        if fn is None:
            return {"error": "bot not ready"}
        try:
            return fn(coin)
        except Exception as e:
            return {"error": str(e)}

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def add_trade(self, trade: dict) -> None:
        with self._lock:
            self.recent_trades = ([trade] + self.recent_trades)[:20]

    def to_dict(self) -> dict:
        with self._lock:
            pct = (
                (self.equity - self.day_open_equity) / self.day_open_equity * 100
                if self.day_open_equity else 0.0
            )
            drawdown_used = (
                max(0.0, (self.day_open_equity - self.equity) / self.day_open_equity * 100)
                if self.day_open_equity else 0.0
            )
            return {
                "equity": self.equity,
                "day_open_equity": self.day_open_equity,
                "equity_pct": round(pct, 3),
                "drawdown_used_pct": round(drawdown_used, 3),
                "kill_switch_pct": KILL_SWITCH_DRAWDOWN * 100,
                "signal": self.signal,
                "active_strategy": self.active_strategy,
                "positions": list(self.positions),
                "news_signals": dict(self.news_signals),
                "fear_greed": self.fear_greed,
                "fear_greed_label": self.fear_greed_label,
                "halted": self.halted,
                "last_tick": self.last_tick,
                "recent_trades": list(self.recent_trades),
            }


bot_state = BotState()
