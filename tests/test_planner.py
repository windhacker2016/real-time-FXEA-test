from fxea.config import PlannerConfig, ScannerConfig, SignalConfig
from fxea.models import Confidence, Direction, Signal, SignalType, Timeframe
from fxea.planner import TradePlanner
from fxea.scanner import MarketScanner
from fxea.signals import SignalEngine

from .helpers import FrameFeed, make_candles


def _uptrend_setup():
    closes = [1.0800]
    for i in range(140):
        closes.append(closes[-1] + (0.0004 if i % 2 == 0 else -0.0002))
    closes.append(closes[-1] + 0.0004)
    feed = FrameFeed({"EURUSD": make_candles(closes)})
    res = MarketScanner(feed, ScannerConfig()).scan("EURUSD")
    sigs = SignalEngine(SignalConfig()).detect(res.snapshot, res.candles)
    sig = next(s for s in sigs if s.direction is Direction.LONG)
    return res, sig


def test_long_plan_geometry():
    res, sig = _uptrend_setup()
    plan = TradePlanner(PlannerConfig()).build(sig, res.snapshot, res.candles)
    assert plan.direction is Direction.LONG
    assert plan.stop_loss < plan.entry_price < plan.take_profit
    assert plan.invalidation_level < plan.stop_loss  # 失效價位在停損之外
    assert plan.entry_zone_low <= plan.entry_price <= plan.entry_zone_high
    assert plan.price_in_entry_zone(plan.entry_price)
    assert plan.is_invalidated(plan.invalidation_level - 0.0001)
    assert not plan.is_invalidated(plan.entry_price)
    if plan.valid:
        assert plan.risk_reward >= PlannerConfig().min_risk_reward
    else:
        assert "風險報酬比" in (plan.invalid_reason or "")
    assert plan.rationale and plan.invalidation_condition


def test_short_plan_mirrors_long():
    closes = [1.1200]
    for i in range(140):
        closes.append(closes[-1] - (0.0004 if i % 2 == 0 else -0.0002))
    closes.append(closes[-1] - 0.0004)
    feed = FrameFeed({"EURUSD": make_candles(closes)})
    res = MarketScanner(feed, ScannerConfig()).scan("EURUSD")
    sig = Signal(symbol="EURUSD", timeframe=Timeframe.H1, signal_type=SignalType.TREND_CONTINUATION, direction=Direction.SHORT, score=75, reference_price=res.snapshot.price)
    plan = TradePlanner(PlannerConfig()).build(sig, res.snapshot, res.candles)
    assert plan.take_profit < plan.entry_price < plan.stop_loss
    assert plan.invalidation_level > plan.stop_loss
    assert plan.is_invalidated(plan.invalidation_level + 0.0001)


def test_confidence_follows_score():
    res, sig = _uptrend_setup()
    planner = TradePlanner(PlannerConfig())
    hi = planner.build(sig.model_copy(update={"score": 85.0}), res.snapshot, res.candles)
    lo = planner.build(sig.model_copy(update={"score": 62.0}), res.snapshot, res.candles)
    assert hi.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    assert lo.confidence is Confidence.LOW


def test_min_rr_invalidates_plan():
    res, sig = _uptrend_setup()
    plan = TradePlanner(PlannerConfig(min_risk_reward=50.0)).build(sig, res.snapshot, res.candles)
    assert not plan.valid
