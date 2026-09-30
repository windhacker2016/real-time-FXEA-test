import pandas as pd

from fxea.indicators import atr, detect_trend, ema, resample_h1_to_h4, rsi, swing_levels, volume_ratio
from fxea.models import Trend

from .helpers import make_candles


def test_ema_and_atr_positive():
    df = make_candles([1.0 + i * 0.001 for i in range(60)])
    assert ema(df["close"], 20).iloc[-1] < df["close"].iloc[-1]
    assert atr(df).iloc[-1] > 0


def test_rsi_bounds_and_direction():
    up = make_candles([1.0 + i * 0.001 for i in range(40)])
    down = make_candles([1.1 - i * 0.001 for i in range(40)])
    assert 90 <= rsi(up["close"]).iloc[-1] <= 100
    assert 0 <= rsi(down["close"]).iloc[-1] <= 10


def test_swing_levels_exclude_last_bar():
    df = make_candles([1.0] * 30 + [1.05])
    hi, lo = swing_levels(df, lookback=20, exclude_last=1)
    assert hi < 1.05  # 最後一根突破棒不算在結構內
    assert lo == df["low"].iloc[:-1].tail(20).min()


def test_volume_ratio():
    df = make_candles([1.0] * 25, volumes=[1000.0] * 24 + [2500.0])
    assert abs(volume_ratio(df, 20) - 2.5) < 1e-9


def test_detect_trend():
    up = make_candles([1.0 + i * 0.0005 for i in range(120)])
    down = make_candles([1.2 - i * 0.0005 for i in range(120)])
    flat = make_candles([1.0 + (0.0001 if i % 2 else -0.0001) for i in range(120)])
    assert detect_trend(up) is Trend.UP
    assert detect_trend(down) is Trend.DOWN
    assert detect_trend(flat) is Trend.SIDEWAYS


def test_resample_h1_to_h4():
    df = make_candles([1.0 + i * 0.001 for i in range(10)])
    h4 = resample_h1_to_h4(df)
    assert len(h4) == 2  # 10 根 → 取後 8 根合併成 2 根
    assert h4["open"].iloc[0] == df["open"].iloc[2]
    assert h4["close"].iloc[-1] == df["close"].iloc[-1]
    assert h4["high"].iloc[-1] == df["high"].iloc[-4:].max()
    assert isinstance(h4, pd.DataFrame)
