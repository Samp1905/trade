from threading import Lock
from typing import Optional

KILL_SWITCH_DRAWDOWN = 0.05


class BotState:
    def __init__(self) -> None:
        self._lock = Lock()
        self.equity: float = 0.0
        self.day_open_equity: float = 0.0
        self.price: float = 0.0
        self.signal: str = "—"
        self.position_side: Optional[str] = None
        self.position_size: float = 0.0
        self.position_entry_px: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.halted: bool = False
        self.last_tick: Optional[float] = None
        self.recent_trades: list = []

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def add_trade(self, trade: dict) -> None:
        with self._lock:
            self.recent_trades = ([trade] + self.recent_trades)[:10]

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
                "price": self.price,
                "signal": self.signal,
                "position_side": self.position_side,
                "position_size": self.position_size,
                "position_entry_px": self.position_entry_px,
                "unrealized_pnl": self.unrealized_pnl,
                "halted": self.halted,
                "last_tick": self.last_tick,
                "recent_trades": list(self.recent_trades),
            }


bot_state = BotState()
