from fxea.config import ScannerConfig, SignalConfig
from fxea.indicators import ema
from fxea.models import Direction, SignalType, Timeframe
from fxea.scanner import MarketScanner
from fxea.signals import SignalEngine

from .helpers import FrameFeed, make_candles


def _detect(df, symbol="EURUSD", min_score=60.0):
    feed = FrameFeed({symbol: df})
    res = MarketScanner(feed, ScannerConfig()).scan(symbol)
    sigs = SignalEngine(SignalConfig(min_score=min_score, max_per_symbol=10)).detect(res.snapshot, res.candles)
    return res, sigs


def _types(sigs, tf=Timeframe.H1):
    return {(s.signal_type, s.direction) for s in sigs if s.timeframe is tf}


def test_breakout_long_detected():
    closes = [1.0800 + i * 0.0002 for i in range(100)]
    closes.append(closes[-1] + 0.0012)  # 突破前 20 根高點,但不到 2 ATR
    vols = [1000.0] * 100 + [2500.0]
    res, sigs = _detect(make_candles(closes, vols))
    assert res.snapshot.opportunity
    assert (SignalType.BREAKOUT, Direction.LONG) in _types(sigs)
    bo = next(s for s in sigs if s.signal_type is SignalType.BREAKOUT)
    assert bo.score >= 70 and bo.key_level is not None
    assert any("成交量" in r for r in bo.reasons)


def test_pullback_long_detected():
    closes = [1.0800 + i * 0.0002 for i in range(101)]
    df = make_candles(closes)
    e20 = float(ema(df["close"], 20).iloc[-1])
    prev_close = df["close"].iloc[-2]
    # 最後一根:下影線回測 EMA20 後收紅
    df.loc[df.index[-1], ["open", "close", "low", "high"]] = [prev_close, prev_close + 0.0004, e20 - 0.0001, prev_close + 0.0006]
    _, sigs = _detect(df)
    assert (SignalType.PULLBACK, Direction.LONG) in _types(sigs)


def test_momentum_long_detected():
    closes = [1.0800 + (0.0001 if i % 2 else -0.0001) for i in range(100)]
    for k in range(1, 5):
        closes.append(closes[99] + k * 0.0010)
    vols = [1000.0] * 100 + [1500.0, 1800.0, 2200.0, 2600.0]
    _, sigs = _detect(make_candles(closes, vols))
    assert (SignalType.MOMENTUM, Direction.LONG) in _types(sigs)


def test_trend_continuation_long_detected():
    closes = [1.0800]
    for i in range(140):
        closes.append(closes[-1] + (0.0004 if i % 2 == 0 else -0.0002))
    closes.append(closes[-1] + 0.0004)
    _, sigs = _detect(make_candles(closes))
    assert (SignalType.TREND_CONTINUATION, Direction.LONG) in _types(sigs)


def test_reversal_long_detected():
    closes = [1.1000 - i * 0.0002 for i in range(101)]
    df = make_candles(closes)
    prev_close = df["close"].iloc[-2]
    # 錘子線:小實體、長下影線、位於支撐附近
    df.loc[df.index[-1], ["open", "close", "low", "high"]] = [prev_close, prev_close + 0.0001, prev_close - 0.0010, prev_close + 0.0002]
    _, sigs = _detect(df)
    assert (SignalType.REVERSAL, Direction.LONG) in _types(sigs)


def test_ranking_and_min_score_filter():
    closes = [1.0800 + i * 0.0002 for i in range(100)] + [1.0800 + 99 * 0.0002 + 0.0012]
    _, sigs = _detect(make_candles(closes, [1000.0] * 100 + [2500.0]), min_score=0.0)
    scores = [s.score for s in sigs]
    assert scores == sorted(scores, reverse=True)
    _, strict = _detect(make_candles(closes), min_score=99.0)
    assert strict == []
