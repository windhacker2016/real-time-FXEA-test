"""AI 交易員層:界限內調整、信念部位、採納建議、看得到 K 棒與戰績、費用追蹤。"""
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from fxea.alerts import MemoryAlertSink
from fxea.app import build_loop
from fxea.approval import ApprovalGate
from fxea.config import ApprovalConfig, PlannerConfig, ResearchConfig, load_settings
from fxea.models import AnalystOutput, Decision, MemoStatus, ResearchAssessment, Timeframe
from fxea.planner import apply_research_adjustments
from fxea.research import ClaudeAnalyst, ResearchContext
from fxea.state import StateStore

from .helpers import T0, make_candles
from .test_memo_approval import _memo
from .test_research import _ctx, _parsed
from .test_risk import plan


def _out(**kw) -> AnalystOutput:
    base = dict(trend_aligned=True, market_regime="trending", confidence_score=4, strength="strong", max_loss_within_plan=True, recommendation="execute", rationale="ok")
    base.update(kw)
    return AnalystOutput(**base)


# ---- 界限內調整 ------------------------------------------------------------


def test_adjustments_within_bounds_applied():
    # 原計畫:進場 1.0850、停損 1.0800(50 點 = 5 ATR → 由規劃器界限外,但這裡直接給)、目標 1.0960、ATR 0.0010
    new, applied, ignored = apply_research_adjustments(plan(), _out(suggested_stop_loss=1.0835, suggested_take_profit=1.0900), PlannerConfig())
    assert not ignored and len(applied) == 2
    assert new.stop_loss == 1.0835 and new.take_profit == 1.09 and new.risk_reward == 3.33
    assert new.invalidation_level == 1.083 and (new.entry_zone_low, new.entry_zone_high) == (1.08475, 1.08525)
    assert new.valid and any(r.startswith("AI 調整") for r in new.rationale)


def test_adjustments_out_of_bounds_ignored():
    out = _out(suggested_entry=1.0900, suggested_stop_loss=1.0849, suggested_take_profit=1.0870)
    new, applied, ignored = apply_research_adjustments(plan(), out, PlannerConfig())
    assert new == plan() and applied == [] and len(ignored) == 3
    assert "ATR" in ignored[0] and "ATR" in ignored[1] and "R:R" in ignored[2]


def test_adjustments_geometry_fallback():
    # 進場下移 0.8 ATR 可採用,但和原停損的距離變成 4.2 ATR > 3 ATR → 整組退回
    new, applied, ignored = apply_research_adjustments(plan(), _out(suggested_entry=1.0842), PlannerConfig())
    assert new == plan() and applied == [] and "幾何不成立" in ignored[-1]


# ---- 核准閘門採納研究建議 -------------------------------------------------


def test_gate_honors_research(tmp_path):
    store = StateStore(tmp_path)
    gate = ApprovalGate(store, ApprovalConfig(mode="auto", honor_research=True, pending_ttl_minutes=60))
    outcomes = {}
    for rec in ("execute", "watch", "skip"):
        m = _memo(store)
        m.research.recommendation = rec
        outcomes[rec] = gate.submit(m, T0)
    assert outcomes["execute"].decision is Decision.APPROVE and outcomes["execute"].decided_by == "auto"
    assert outcomes["watch"].decision is Decision.WATCHLIST and outcomes["watch"].decided_by == "auto:research"
    assert outcomes["skip"].decision is Decision.REJECT and outcomes["skip"].note == "研究建議略過"
    assert gate.active_symbols() == {"EURUSD"}  # 觀察中 + 已核准未執行
    # 自動觀察清單逾時失效
    from datetime import timedelta

    expired = gate.expire_stale(T0 + timedelta(minutes=61))
    assert [m.memo_id for m in expired] == [outcomes["watch"].memo_id]

    plain = ApprovalGate(store, ApprovalConfig(mode="auto", honor_research=False))
    m = _memo(store)
    m.research.recommendation = "skip"
    assert plain.submit(m, T0).decision is Decision.APPROVE


# ---- Claude 看得到什麼、能決定什麼、花多少 -----------------------------------


class _Msgs:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def parse(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.response


def _client(response=None, error=None):
    m = _Msgs(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=m)), m


def _resp(parsed, **usage):
    u = dict(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    u.update(usage)
    return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, model="claude-fable-5-1", usage=SimpleNamespace(**u))


def test_payload_has_candles_recent_trades_and_usage_is_tracked():
    ctx = _ctx()
    ctx.candles = {Timeframe.H1: make_candles([1.08 + i * 1e-4 for i in range(80)]), Timeframe.H4: make_candles([1.07 + i * 5e-4 for i in range(50)])}
    ctx.recent_trades = [{"memo_id": "FD-001-0302", "signal": "pullback", "pnl": -93.2, "r_multiple": -1.0, "reason": "停損"}]
    client, msgs = _client(_resp(_parsed(risk_fraction=0.1, suggested_stop_loss=1.0835), input_tokens=1000, output_tokens=200, cache_read_input_tokens=500))
    analyst = ClaudeAnalyst(ResearchConfig(model="claude-fable-5-1", candles_h1=60, candles_h4=40), client=client)
    out = analyst.assess(ctx)

    content = msgs.calls[0]["messages"][0]["content"]
    for needle in ("candles_h1_csv", "candles_h4_csv", "indicators_h1", "indicators_h4", "recent_trades_this_symbol", "FD-001-0302", "mechanical_plan", "risk_checks"):
        assert needle in content
    assert content.count("2026-03-") >= 100  # 真的把 K 棒列出來(60 根 H1 + 40 根 H4)
    assert "watch" in msgs.calls[0]["system"][0]["text"] and "suggested_entry" in msgs.calls[0]["system"][0]["text"]

    assert out.risk_fraction == 0.25  # 0.1 → 夾到下限
    assert out.suggested_stop_loss == 1.0835 and out.tokens_in == 1500 and out.tokens_out == 200
    assert analyst.calls == 1 and analyst.usage["input_tokens"] == 1000 and analyst.usage["cache_read_input_tokens"] == 500
    assert abs(analyst.estimated_cost_usd - (1000 * 10 + 200 * 50 + 500 * 0.25) / 1e6) < 1e-9


def test_capabilities_can_be_disabled():
    client, _ = _client(_resp(_parsed(risk_fraction=2.0, suggested_entry=1.0, suggested_stop_loss=1.0, suggested_take_profit=1.1)))
    out = ClaudeAnalyst(ResearchConfig(allow_plan_adjustment=False, allow_conviction_sizing=False), client=client).assess(_ctx())
    assert out.risk_fraction == 1.0 and out.suggested_entry is None and out.suggested_stop_loss is None and out.suggested_take_profit is None
    client2, _ = _client(_resp(_parsed(risk_fraction=2.0)))
    assert ClaudeAnalyst(ResearchConfig(), client=client2).assess(_ctx()).risk_fraction == 1.0  # 2.0 → 夾到上限


def test_strict_mode_raises_instead_of_fallback():
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client, _ = _client(error=err)
    with pytest.raises(RuntimeError):
        ClaudeAnalyst(ResearchConfig(), client=client, strict=True).assess(_ctx())
    lenient = ClaudeAnalyst(ResearchConfig(), client=client, strict=False).assess(_ctx())
    assert lenient.source.startswith("rules(fallback")


# ---- 監控循環整合:AI 的判斷有牙齒 ----------------------------------------------


class _FakeAnalyst:
    name = "fake"

    def __init__(self, recommendation="execute", fraction=1.0, adjust=False):
        self.recommendation, self.fraction, self.adjust = recommendation, fraction, adjust
        self.seen_recent_trades = 0
        self.seen_candles = False

    def assess(self, ctx: ResearchContext) -> ResearchAssessment:
        self.seen_recent_trades = max(self.seen_recent_trades, len(ctx.recent_trades))
        self.seen_candles = self.seen_candles or bool(ctx.candles)
        p = ctx.plan
        kw = {}
        if self.adjust:
            sgn = p.direction.sign
            kw = dict(suggested_stop_loss=p.entry_price - sgn * 1.0 * p.atr, suggested_take_profit=p.entry_price + sgn * 2.5 * p.atr)
        return ResearchAssessment(
            trend_aligned=True,
            market_regime="trending",
            confidence_score=4,
            strength="strong",
            max_loss_within_plan=True,
            recommendation=self.recommendation,
            rationale="fake",
            risk_fraction=self.fraction,
            source="fake",
            **kw,
        )


def _settings(tmp_path, **over):
    base = {
        "watchlist": ["EURUSD", "GBPUSD", "USDJPY"],
        "state_dir": str(tmp_path / "state"),
        "approval": {"mode": "auto", "honor_research": True},
        "alerts": {"console": False, "file": None},
    }
    for k, v in over.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return load_settings(None, base)


def test_loop_applies_adjustments_and_conviction_sizing(tmp_path):
    analyst = _FakeAnalyst(fraction=0.5, adjust=True)
    loop = build_loop(_settings(tmp_path), alerts=MemoryAlertSink(), analyst=analyst)
    loop.run(cycles=80, interval=0)
    ready = [m for m in loop.state.list_memos() if m.status is MemoStatus.READY]
    assert ready and analyst.seen_candles
    for m in ready:
        assert m.research.applied_adjustments, m.research.ignored_suggestions
        sgn = m.plan.direction.sign
        assert abs((m.plan.entry_price - m.plan.stop_loss) * sgn - 1.0 * m.plan.atr) < m.plan.atr * 0.05
        assert m.plan.risk_reward >= 2.4
        assert m.risk.risk_pct <= 0.55  # 信念部位 ×0.5
        assert any(r.startswith("AI 調整") for r in m.plan.rationale)
    # 戰績回填,且後續的 AI 看得到
    closed = [m for m in loop.state.list_memos() if m.outcome_pnl is not None]
    assert closed and all(m.r_multiple is not None for m in closed)
    assert analyst.seen_recent_trades >= 1


def test_loop_skip_recommendation_never_trades(tmp_path):
    loop = build_loop(_settings(tmp_path), alerts=MemoryAlertSink(), analyst=_FakeAnalyst(recommendation="skip"))
    loop.run(cycles=60, interval=0)
    memos = loop.state.list_memos()
    assert memos and all(m.decision is Decision.REJECT for m in memos if m.status is MemoStatus.READY)
    assert loop.broker.all_positions() == []


def test_loop_fail_fast_propagates_analyst_errors(tmp_path):
    class Boom:
        name = "boom"

        def assess(self, ctx):
            raise RuntimeError("Claude 分析失敗")

    loop = build_loop(_settings(tmp_path), alerts=MemoryAlertSink(), analyst=Boom())
    with pytest.raises(RuntimeError):
        loop.run(cycles=40, interval=0, fail_fast=True)
    sink = MemoryAlertSink()
    lenient = build_loop(_settings(tmp_path / "b"), alerts=sink, analyst=Boom())
    lenient.run(cycles=40, interval=0)
    assert any(a.title == "循環錯誤" for a in sink.alerts)
