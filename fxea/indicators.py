"""技術指標(純 pandas / numpy,無外部 TA 相依)。

所有函式接受欄位為 ``time, open, high, low, close, volume`` 的 DataFrame,
最後一列視為「目前」這根 K 棒。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .models import Trend

COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False, min_periods=1).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.fillna(df["high"] - df["low"])


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean()


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0).where(avg_loss > 0, 100.0).where(avg_gain > 0, 50.0).fillna(50.0)


def roc(series: pd.Series, n: int) -> pd.Series:
    """變動率 (%)。"""
    return (series / series.shift(n) - 1.0) * 100.0


def slope(series: pd.Series, n: int = 5) -> float:
    """最近 n 個值的線性回歸斜率(每根 K 棒的變化量)。"""
    tail = series.tail(n).to_numpy(dtype=float)
    if len(tail) < 2:
        return 0.0
    x = np.arange(len(tail), dtype=float)
    return float(np.polyfit(x, tail, 1)[0])


def swing_levels(df: pd.DataFrame, lookback: int = 20, exclude_last: int = 1) -> tuple[float, float]:
    """回傳 (swing_high, swing_low):排除最近 ``exclude_last`` 根後的區間高低點。"""
    window = df.iloc[:-exclude_last] if exclude_last > 0 else df
    window = window.tail(lookback)
    if window.empty:
        window = df.tail(1)
    return float(window["high"].max()), float(window["low"].min())


def volume_ratio(df: pd.DataFrame, n: int = 20) -> float:
    """最新一根成交量 / 前 n 根平均。無成交量資料時回傳 1.0。"""
    if len(df) < 2 or df["volume"].sum() <= 0:
        return 1.0
    prev = df["volume"].iloc[-(n + 1) : -1]
    avg = float(prev.mean()) if len(prev) else 0.0
    if avg <= 0:
        return 1.0
    return float(df["volume"].iloc[-1] / avg)


def percentile_rank(series: pd.Series, value: float) -> float:
    arr = series.dropna().to_numpy(dtype=float)
    if arr.size == 0:
        return 50.0
    return float((arr <= value).mean() * 100.0)


def detect_trend(df: pd.DataFrame, fast: int = 20, slow: int = 50, atr_n: int = 14) -> Trend:
    """EMA 快慢線 + 價格位置判斷趨勢;快慢線距離小於 0.2 ATR 視為盤整。"""
    if len(df) < 5:
        return Trend.SIDEWAYS
    close = df["close"]
    f = float(ema(close, fast).iloc[-1])
    s = float(ema(close, slow).iloc[-1])
    a = float(atr(df, atr_n).iloc[-1])
    price = float(close.iloc[-1])
    if a <= 0 or abs(f - s) < 0.2 * a:
        return Trend.SIDEWAYS
    if f > s and price > s:
        return Trend.UP
    if f < s and price < s:
        return Trend.DOWN
    return Trend.SIDEWAYS


def is_bullish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, cur = df.iloc[-2], df.iloc[-1]
    return (
        prev["close"] < prev["open"]
        and cur["close"] > cur["open"]
        and cur["close"] >= prev["open"]
        and cur["open"] <= prev["close"]
    )


def is_bearish_engulfing(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    prev, cur = df.iloc[-2], df.iloc[-1]
    return (
        prev["close"] > prev["open"]
        and cur["close"] < cur["open"]
        and cur["close"] <= prev["open"]
        and cur["open"] >= prev["close"]
    )


def is_hammer(df: pd.DataFrame) -> bool:
    """下影線至少為實體 2 倍、收盤在上半部。"""
    cur = df.iloc[-1]
    body = abs(cur["close"] - cur["open"])
    rng = cur["high"] - cur["low"]
    if rng <= 0:
        return False
    lower = min(cur["open"], cur["close"]) - cur["low"]
    return lower >= 2 * body and (cur["close"] - cur["low"]) / rng >= 0.6


def is_shooting_star(df: pd.DataFrame) -> bool:
    cur = df.iloc[-1]
    body = abs(cur["close"] - cur["open"])
    rng = cur["high"] - cur["low"]
    if rng <= 0:
        return False
    upper = cur["high"] - max(cur["open"], cur["close"])
    return upper >= 2 * body and (cur["high"] - cur["close"]) / rng >= 0.6


def resample_h1_to_h4(df: pd.DataFrame) -> pd.DataFrame:
    """把 H1 K 棒每 4 根合併成一根 H4(由最舊往最新對齊)。"""
    if df.empty:
        return df.copy()
    n = len(df) - (len(df) % 4)
    if n == 0:
        return df.tail(1).copy().reset_index(drop=True)
    block = df.tail(n).reset_index(drop=True)
    grp = block.index // 4
    out = block.groupby(grp).agg(
        time=("time", "first"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    return out.reset_index(drop=True)
