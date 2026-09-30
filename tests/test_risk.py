from datetime import timedelta

import pytest

from fxea.config import RiskConfig
from fxea.models import Confidence, Direction, NewsEvent, Position, RiskMode, RiskRating, SignalType, Timeframe, TradePlan
from fxea.risk import RiskGovernor, RiskModule
from fxea.state import StateStore

from .helpers import T0, account, snapshot

H = timedelta(hours=1)


def plan(**kw) -> TradePlan:
    base = dict(
        symbol="EURUSD",
        direction=Direction.LONG,
        timeframe=Timeframe.H1,
        signal_type=SignalType.PULLBACK,
        entry_price=1.0850,
        entry_zone_low=1.0847,
        entry_zone_high=1.0853,
        stop_loss=1.0800,  # 50 點
        take_profit=1.0960,  # 110 點
        invalidation_level=1.0795,
        invalidation_condition="x",
        risk_reward=2.2,
        confidence=Confidence.HIGH,
        atr=0.0010,
    )
    base.update(kw)
    return TradePlan(**base)


def position(symbol="GBPUSD", risk_amount=100.0) -> Position:
    return Position(position_id="P-1", symbol=symbol, direction=Direction.LONG, lots=0.2, entry_price=1.27, stop_loss=1.265, take_profit=1.281, opened_at=T0, risk_amount=risk_amount)


def test_all_checks_pass_and_sizing():
    rep = RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(), [], T0)
    assert rep.passed and not rep.blocked_reasons
    assert rep.lots == 0.2  # 100 USD / (50 點 × 10 USD)
    assert rep.risk_amount == 100.0
    assert abs(rep.risk_pct - 1.0) < 1e-6
    assert [c.name for c in rep.checks] == ["position_size", "exposure", "drawdown", "volatility", "max_loss"]
    assert rep.rating is RiskRating.LOW


def test_jpy_pip_value():
    p = plan(symbol="USDJPY", entry_price=150.00, stop_loss=149.50, take_profit=151.10, invalidation_level=149.40)
    rep = RiskModule(RiskConfig()).evaluate(p, snapshot("USDJPY", 150.0), account(), [], T0)
    assert rep.passed
    assert rep.lots == 0.3  # 100 / (50 點 × 6.67 USD) = 0.30


def test_exposure_cap_blocks():
    cfg = RiskConfig(max_open_positions=2)
    opens = [position("GBPUSD"), position("AUDUSD")]
    rep = RiskModule(cfg).evaluate(plan(), snapshot(), account(), opens, T0)
    assert not rep.passed and not rep.check("exposure").passed
    assert "上限" in rep.check("exposure").detail


def test_same_symbol_blocks():
    rep = RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(), [position("EURUSD")], T0)
    assert not rep.check("exposure").passed
    ok = RiskModule(RiskConfig(allow_same_symbol=True, max_daily_loss_pct=5.0)).evaluate(plan(), snapshot(), account(), [position("EURUSD")], T0)
    assert ok.check("exposure").passed


def test_drawdown_blocks():
    rep = RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(equity=8900.0, peak=10000.0), [], T0)
    assert not rep.check("drawdown").passed
    assert rep.rating is RiskRating.HIGH


def test_volatility_blocks_on_atr_percentile_and_news():
    rep = RiskModule(RiskConfig()).evaluate(plan(), snapshot(atr_percentile=97.0), account(), [], T0)
    assert not rep.check("volatility").passed
    news = [NewsEvent(time=T0 + timedelta(minutes=10), currency="USD", impact="high", title="NFP")]
    rep2 = RiskModule(RiskConfig()).evaluate(plan(), snapshot(upcoming_news=news), account(), [], T0)
    assert not rep2.check("volatility").passed and "新聞" in rep2.check("volatility").detail
    far = [NewsEvent(time=T0 + timedelta(hours=3), currency="USD", impact="high", title="FOMC")]
    rep3 = RiskModule(RiskConfig()).evaluate(plan(), snapshot(upcoming_news=far), account(), [], T0)
    assert rep3.check("volatility").passed


def test_max_daily_loss_includes_open_risk():
    rep = RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(daily_pnl=-250.0), [], T0)
    assert not rep.check("max_loss").passed
    # 今日 0 虧損,但兩筆未平倉各 100 + 本筆 100 = 300 = 上限 → 通過;三筆則超標
    opens = [position("GBPUSD"), position("AUDUSD")]
    assert RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(), opens, T0).check("max_loss").passed
    opens.append(position("USDJPY"))
    assert not RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(), opens, T0).check("max_loss").passed


def test_invalid_plan_fails_position_size():
    rep = RiskModule(RiskConfig()).evaluate(plan(valid=False, invalid_reason="R:R 太低"), snapshot(), account(), [], T0)
    assert not rep.passed and "無效" in rep.check("position_size").detail


def test_too_small_account_yields_zero_lots():
    rep = RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(equity=50.0), [], T0)
    assert rep.lots == 0.0 and not rep.check("position_size").passed


# ---- 回撤斷路器 ----------------------------------------------------------


def test_governor_halt_cooldown_recovery_normal(tmp_path):
    cfg = RiskConfig(max_drawdown_pct=10.0, drawdown_cooldown_hours=24, recovery_risk_scale=0.5)
    g = RiskGovernor(cfg, StateStore(tmp_path))
    assert g.update(account(10_000.0), T0) == [] and g.state.reference_peak == 10_000.0
    assert g.update(account(11_000.0, peak=11_000.0), T0 + H) == [] and g.state.reference_peak == 11_000.0

    (t,) = g.update(account(9_800.0, peak=11_000.0), T0 + 2 * H)  # 回撤 10.9%
    assert t.to_mode is RiskMode.HALTED and g.halted and g.risk_scale == 0.0 and g.state.halts == 1
    assert "10.91%" in t.reason
    assert g.update(account(9_800.0, peak=11_000.0), T0 + 10 * H) == []  # 冷卻中,不再轉換

    (t2,) = g.update(account(9_800.0, peak=11_000.0), T0 + 26 * H)  # 冷卻 24h 期滿
    assert t2.to_mode is RiskMode.RECOVERY and t2.by == "auto"
    assert g.state.reference_peak == 9_800.0 and g.risk_scale == 0.5 and g.state.peak_at_halt == 11_000.0
    assert g.update(account(9_500.0, peak=11_000.0), T0 + 27 * H) == []  # 從新高水位算,回撤 3%
    assert abs(g.drawdown_pct(9_500.0) - 3.0612) < 0.01

    (t3,) = g.update(account(11_000.0, peak=11_000.0), T0 + 28 * H)  # 回到暫停前高點
    assert t3.to_mode is RiskMode.NORMAL and g.risk_scale == 1.0

    g2 = RiskGovernor(cfg, StateStore(tmp_path))  # 持久化
    assert g2.mode is RiskMode.NORMAL and g2.state.halts == 1


def test_governor_manual_resume_only(tmp_path):
    cfg = RiskConfig(max_drawdown_pct=10.0, drawdown_cooldown_hours=0)
    g = RiskGovernor(cfg, StateStore(tmp_path))
    g.update(account(10_000.0), T0)
    g.update(account(8_900.0, peak=10_000.0), T0 + H)
    assert g.halted
    assert g.update(account(8_900.0, peak=10_000.0), T0 + 1000 * H) == []  # 永遠不自動恢復
    t = g.resume(T0 + 1001 * H, by="alice", full_risk=True)
    assert t.to_mode is RiskMode.NORMAL and g.state.resumed_by == "alice" and g.state.reference_peak == 8_900.0
    with pytest.raises(ValueError):
        g.resume(T0 + 1002 * H)


def test_evaluate_with_governor_inputs():
    half = RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(), [], T0, risk_scale=0.5)
    assert half.passed and half.lots == 0.1 and half.risk_amount == 50.0
    dd = RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(), [], T0, drawdown_pct=12.0)
    assert not dd.check("drawdown").passed
    halted = RiskModule(RiskConfig()).evaluate(plan(), snapshot(), account(), [], T0, mode=RiskMode.HALTED, drawdown_pct=10.5)
    assert not halted.check("drawdown").passed and "暫停" in halted.check("drawdown").detail
