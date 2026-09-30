"""執行層介面:帳戶、部位、下單、平倉、逐筆更新。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import Account, Position, TradePlan


class Broker(ABC):
    name: str = "broker"

    @abstractmethod
    def account(self) -> Account: ...

    @abstractmethod
    def open_positions(self) -> list[Position]: ...

    @abstractmethod
    def open_position(
        self,
        plan: TradePlan,
        lots: float,
        price: float,
        now: datetime,
        memo_id: str | None = None,
        risk_amount: float = 0.0,
    ) -> Position: ...

    @abstractmethod
    def close_position(self, position_id: str, price: float, now: datetime, reason: str) -> Position: ...

    @abstractmethod
    def mark_to_market(self, prices: dict[str, float], now: datetime) -> list[Position]:
        """用最新價更新權益;回傳這一輪被平倉(停損/停利)的部位。"""
