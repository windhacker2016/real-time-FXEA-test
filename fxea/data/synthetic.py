"""可重現的合成行情:用於示範、測試與紙上交易。

以隨機漫步加上「趨勢段」產生 H1 K 棒;H4 由 H1 合併而成。
``advance()`` 每次追加一根新 H1,讓監控循環看得到市場在動。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..indicators import COLUMNS, resample_h1_to_h4
from ..models import Timeframe
from .base import MarketDataFeed

_BASE_PRICES = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2700,
    "USDJPY": 150.00,
    "AUDUSD": 0.6550,
    "USDCHF": 0.8800,
    "USDCAD": 1.3600,
    "NZDUSD": 0.6000,
}


class SyntheticFeed(MarketDataFeed):
    def __init__(
        self,
        symbols: list[str],
        seed: int = 42,
        bars: int = 600,
        start: datetime | None = None,
        regime_len: int = 80,
    ):
        self.symbols = [s.upper() for s in symbols]
        self.seed = seed
        self.regime_len = regime_len
        self._rng = np.random.default_rng(seed)
        self._start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._h1: dict[str, pd.DataFrame] = {}
        self._state: dict[str, dict] = {}
        for i, sym in enumerate(self.symbols):
            self._h1[sym] = self._generate(sym, bars, i)

    # ------------------------------------------------------------------
    def _pip(self, symbol: str) -> float:
        return 0.01 if symbol.endswith("JPY") else 0.0001

    def _new_regime(self, symbol: str) -> None:
        pip = self._pip(symbol)
        drift = float(self._rng.choice([-1.0, -0.5, 0.0, 0.5, 1.0])) * 3.0 * pip
        vol = float(self._rng.uniform(8.0, 18.0)) * pip
        self._state[symbol] = {"drift": drift, "vol": vol, "left": self.regime_len}

    def _next_bar(self, symbol: str, prev_close: float, t: datetime) -> dict:
        st = self._state.get(symbol)
        if st is None or st["left"] <= 0:
            self._new_regime(symbol)
            st = self._state[symbol]
        st["left"] -= 1
        pip = self._pip(symbol)
        move = st["drift"] + self._rng.normal(0.0, st["vol"])
        open_ = prev_close
        close = open_ + move
        wick_up = abs(self._rng.normal(0.0, st["vol"] * 0.5))
        wick_dn = abs(self._rng.normal(0.0, st["vol"] * 0.5))
        high = max(open_, close) + wick_up
        low = min(open_, close) - wick_dn
        base_vol = 1000.0
        volume = base_vol * float(self._rng.lognormal(0.0, 0.35)) * (1.0 + abs(move) / (st["vol"] + pip))
        return {
            "time": t,
            "open": round(open_, 5),
            "high": round(high, 5),
            "low": round(low, 5),
            "close": round(close, 5),
            "volume": round(volume, 1),
        }

    def _generate(self, symbol: str, bars: int, offset: int) -> pd.DataFrame:
        price = _BASE_PRICES.get(symbol, 1.0)
        rows = []
        t = self._start - timedelta(hours=bars)
        for _ in range(bars):
            row = self._next_bar(symbol, price, t)
            rows.append(row)
            price = row["close"]
            t += timedelta(hours=1)
        return pd.DataFrame(rows, columns=COLUMNS)

    # ------------------------------------------------------------------
    def candles(self, symbol: str, timeframe: Timeframe, n: int) -> pd.DataFrame:
        sym = symbol.upper()
        if sym not in self._h1:
            self.symbols.append(sym)
            self._h1[sym] = self._generate(sym, 600, len(self.symbols))
        h1 = self._h1[sym]
        if timeframe is Timeframe.H1:
            return h1.tail(n).reset_index(drop=True)
        if timeframe is Timeframe.H4:
            return resample_h1_to_h4(h1).tail(n).reset_index(drop=True)
        raise ValueError(f"SyntheticFeed 只支援 H1/H4,收到 {timeframe}")

    def price(self, symbol: str) -> float:
        return float(self._h1[symbol.upper()]["close"].iloc[-1])

    def now(self) -> datetime:
        first = next(iter(self._h1.values()))
        return first["time"].iloc[-1].to_pydatetime() + timedelta(hours=1)

    def advance(self) -> None:
        for sym, df in self._h1.items():
            last = df.iloc[-1]
            t = last["time"].to_pydatetime() + timedelta(hours=1)
            row = self._next_bar(sym, float(last["close"]), t)
            self._h1[sym] = pd.concat([df, pd.DataFrame([row], columns=COLUMNS)], ignore_index=True)

    # 測試/示範用:強制某商品進入指定趨勢段
    def force_regime(self, symbol: str, drift_pips: float, vol_pips: float, bars: int) -> None:
        sym = symbol.upper()
        pip = self._pip(sym)
        self._state[sym] = {"drift": drift_pips * pip, "vol": vol_pips * pip, "left": bars}
