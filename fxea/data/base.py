"""資料來源介面:行情與新聞。"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from ..models import NewsEvent, Timeframe


class MarketDataFeed(ABC):
    @abstractmethod
    def candles(self, symbol: str, timeframe: Timeframe, n: int) -> pd.DataFrame:
        """回傳最近 n 根 K 棒,欄位 time/open/high/low/close/volume,最後一列最新。"""

    @abstractmethod
    def price(self, symbol: str) -> float:
        """最新成交價。"""

    @abstractmethod
    def now(self) -> datetime:
        """資料來源的「現在」(回放來源會是歷史時間)。"""

    def advance(self) -> None:
        """回放型來源前進一根;即時來源不需實作。"""


class NewsFeed(ABC):
    @abstractmethod
    def upcoming(self, now: datetime, horizon_minutes: int, currencies: set[str] | None = None) -> list[NewsEvent]:
        """回傳 now 之後 horizon 分鐘內(含剛發生 15 分鐘內)的事件。"""


class StaticNewsFeed(NewsFeed):
    """從清單或 JSON 檔載入的固定事件表(圖 2「新聞:監控新聞與事件」)。"""

    def __init__(self, events: list[NewsEvent] | None = None):
        self.events: list[NewsEvent] = sorted(events or [], key=lambda e: e.time)

    @classmethod
    def from_file(cls, path: str | Path) -> "StaticNewsFeed":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([NewsEvent.model_validate(item) for item in raw])

    def upcoming(self, now: datetime, horizon_minutes: int, currencies: set[str] | None = None) -> list[NewsEvent]:
        start = now - timedelta(minutes=15)
        end = now + timedelta(minutes=horizon_minutes)
        out = [e for e in self.events if start <= e.time <= end]
        if currencies:
            out = [e for e in out if e.currency.upper() in currencies]
        return out
