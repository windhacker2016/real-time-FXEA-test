"""CSV 回放來源:``{dir}/{SYMBOL}_H1.csv``,欄位 time,open,high,low,close,volume。

``advance()`` 讓游標前進一根,可用歷史資料重播整個監控循環。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..indicators import COLUMNS, resample_h1_to_h4
from ..models import Timeframe
from .base import MarketDataFeed


class CsvFeed(MarketDataFeed):
    def __init__(
        self,
        directory: str | Path,
        symbols: list[str],
        warmup: int = 300,
        warmup_until: datetime | None = None,
    ):
        """``warmup``:至少先給指標多少根歷史;``warmup_until``:從這個時間點才開始「現在」。"""
        self.dir = Path(directory)
        self.symbols = [s.upper() for s in symbols]
        self._h1: dict[str, pd.DataFrame] = {}
        self._cursor: dict[str, int] = {}
        for sym in self.symbols:
            path = self.dir / f"{sym}_H1.csv"
            if not path.exists():
                raise FileNotFoundError(path)
            df = pd.read_csv(path)
            missing = [c for c in COLUMNS if c not in df.columns]
            if missing:
                raise ValueError(f"{path} 缺少欄位 {missing}")
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df = df[COLUMNS].sort_values("time").reset_index(drop=True)
            self._h1[sym] = df
            cursor = min(warmup, len(df))
            if warmup_until is not None:
                cursor = max(cursor, int((df["time"] < pd.Timestamp(warmup_until)).sum()))
            self._cursor[sym] = min(cursor, len(df))

    def _visible(self, sym: str) -> pd.DataFrame:
        return self._h1[sym].iloc[: self._cursor[sym]]

    def candles(self, symbol: str, timeframe: Timeframe, n: int) -> pd.DataFrame:
        sym = symbol.upper()
        h1 = self._visible(sym)
        if timeframe is Timeframe.H1:
            return h1.tail(n).reset_index(drop=True)
        if timeframe is Timeframe.H4:
            return resample_h1_to_h4(h1).tail(n).reset_index(drop=True)
        raise ValueError(f"CsvFeed 只支援 H1/H4,收到 {timeframe}")

    def price(self, symbol: str) -> float:
        return float(self._visible(symbol.upper())["close"].iloc[-1])

    def now(self) -> datetime:
        sym = self.symbols[0]
        last = self._visible(sym)["time"].iloc[-1].to_pydatetime()
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return last + timedelta(hours=1)

    def advance(self) -> None:
        for sym in self.symbols:
            if self._cursor[sym] < len(self._h1[sym]):
                self._cursor[sym] += 1

    @property
    def exhausted(self) -> bool:
        return all(self._cursor[s] >= len(self._h1[s]) for s in self.symbols)
