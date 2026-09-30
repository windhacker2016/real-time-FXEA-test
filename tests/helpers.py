"""測試用工具:手工 K 棒序列、固定資料來源。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from fxea.data.base import MarketDataFeed
from fxea.indicators import COLUMNS, resample_h1_to_h4
from fxea.models import Account, KeyLevels, MarketSnapshot, Timeframe, Trend

T0 = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)


def make_candles(closes: list[float], volumes: list[float] | None = None, wick: float = 0.0003, start: datetime = T0) -> pd.DataFrame:
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append(
            {
                "time": start + timedelta(hours=i),
                "open": o,
                "high": max(o, c) + wick,
                "low": min(o, c) - wick,
                "close": c,
                "volume": volumes[i] if volumes else 1000.0,
            }
        )
        prev = c
    return pd.DataFrame(rows, columns=COLUMNS)


class FrameFeed(MarketDataFeed):
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = {k.upper(): v for k, v in frames.items()}

    def candles(self, symbol: str, timeframe: Timeframe, n: int) -> pd.DataFrame:
        h1 = self.frames[symbol.upper()]
        if timeframe is Timeframe.H1:
            return h1.tail(n).reset_index(drop=True)
        return resample_h1_to_h4(h1).tail(n).reset_index(drop=True)

    def price(self, symbol: str) -> float:
        return float(self.frames[symbol.upper()]["close"].iloc[-1])

    def now(self) -> datetime:
        first = next(iter(self.frames.values()))
        return first["time"].iloc[-1].to_pydatetime() + timedelta(hours=1)


def snapshot(symbol: str = "EURUSD", price: float = 1.0850, **kw) -> MarketSnapshot:
    base = dict(
        symbol=symbol,
        time=T0,
        price=price,
        change_pct=0.1,
        volume_ratio=1.3,
        trend_h1=Trend.UP,
        trend_h4=Trend.UP,
        atr_h1=0.0010,
        atr_h4=0.0025,
        atr_percentile=50.0,
        rsi_h1=58.0,
        levels=KeyLevels(support=1.0780, resistance=1.0960, swing_low=1.0800, swing_high=1.0900),
        opportunity=True,
    )
    base.update(kw)
    return MarketSnapshot(**base)


def account(equity: float = 10_000.0, balance: float | None = None, peak: float | None = None, daily_pnl: float = 0.0) -> Account:
    return Account(
        currency="USD",
        balance=balance if balance is not None else equity,
        equity=equity,
        peak_equity=peak if peak is not None else equity,
        day=T0.date(),
        daily_pnl=daily_pnl,
    )
