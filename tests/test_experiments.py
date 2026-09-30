"""三個實驗開關:訊號型態、趨勢同向、交易時段;以及 --set 覆寫。"""
from datetime import datetime, timezone

import pytest

from fxea.config import ScannerConfig, SignalConfig, load_settings, parse_set_args
from fxea.models import Direction, SignalType, Trend
from fxea.scanner import MarketScanner
from fxea.signals import SignalEngine

from .helpers import FrameFeed, make_candles


def _breakout_feed():
    closes = [1.0800 + i * 0.0002 for i in range(100)]
    closes.append(closes[-1] + 0.0012)
    return FrameFeed({"EURUSD": make_candles(closes, [1000.0] * 100 + [2500.0])})


def test_enabled_types_filter():
    res = MarketScanner(_breakout_feed(), ScannerConfig()).scan("EURUSD")
    everything = SignalEngine(SignalConfig(max_per_symbol=10)).detect(res.snapshot, res.candles)
    assert {s.signal_type for s in everything} >= {SignalType.BREAKOUT}
    only = SignalEngine(SignalConfig(max_per_symbol=10, enabled_types=[SignalType.BREAKOUT])).detect(res.snapshot, res.candles)
    assert only and all(s.signal_type is SignalType.BREAKOUT for s in only) and len(only) <= len(everything)
    assert SignalEngine(SignalConfig(enabled_types=[SignalType.REVERSAL])).detect(res.snapshot, res.candles) == []


def test_require_trend_alignment():
    res = MarketScanner(_breakout_feed(), ScannerConfig()).scan("EURUSD")
    assert res.snapshot.trend_h4 is Trend.UP
    aligned = SignalEngine(SignalConfig(max_per_symbol=10, require_trend_alignment="h4")).detect(res.snapshot, res.candles)
    assert aligned and all(s.direction is Direction.LONG for s in aligned)
    res.snapshot.trend_h4 = Trend.DOWN  # 假設大週期反向 → 所有做多訊號都被濾掉
    assert SignalEngine(SignalConfig(max_per_symbol=10, require_trend_alignment="h4")).detect(res.snapshot, res.candles) == []
    res.snapshot.trend_h4 = Trend.UP
    res.snapshot.trend_h1 = Trend.SIDEWAYS
    assert SignalEngine(SignalConfig(max_per_symbol=10, require_trend_alignment="both")).detect(res.snapshot, res.candles) == []


def test_trading_hours_session_filter():
    feed = _breakout_feed()
    h = feed.now().hour
    inside = MarketScanner(feed, ScannerConfig(trading_hours_utc=(h, h + 1))).scan("EURUSD")
    assert inside.snapshot.in_session and inside.snapshot.opportunity
    outside = MarketScanner(feed, ScannerConfig(trading_hours_utc=((h + 2) % 24, (h + 3) % 24))).scan("EURUSD")
    assert not outside.snapshot.in_session and not outside.snapshot.opportunity
    assert any("時段外" in n for n in outside.snapshot.notes)

    wrap = MarketScanner(feed, ScannerConfig(trading_hours_utc=(22, 6)))
    at = lambda hour: datetime(2026, 1, 1, hour, tzinfo=timezone.utc)  # noqa: E731
    assert wrap.in_session(at(23)) and wrap.in_session(at(3)) and not wrap.in_session(at(12)) and not wrap.in_session(at(6))


def test_parse_set_args_and_overrides():
    ov = parse_set_args(["signals.min_score=70", "scanner.trading_hours_utc=[7,17]", "signals.enabled_types=[breakout, pullback]", "approval.honor_research=true"])
    s = load_settings(None, ov)
    assert s.signals.min_score == 70
    assert s.scanner.trading_hours_utc == (7, 17)
    assert s.signals.enabled_types == [SignalType.BREAKOUT, SignalType.PULLBACK]
    assert s.approval.honor_research is True
    with pytest.raises(ValueError):
        parse_set_args(["nonsense"])
