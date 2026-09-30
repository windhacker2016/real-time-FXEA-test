from types import SimpleNamespace

import anthropic
import httpx2

from fxea.config import ResearchConfig, RiskConfig
from fxea.models import AnalystOutput, Direction, MarketRegime, Signal, SignalType, Timeframe, Trend
from fxea.research import ClaudeAnalyst, ResearchContext, RuleBasedAnalyst
from fxea.risk import RiskModule

from .helpers import T0, account, snapshot
from .test_risk import plan


def _ctx(score=78.0, **snap_kw):
    snap = snapshot(**snap_kw)
    sig = Signal(symbol="EURUSD", timeframe=Timeframe.H1, signal_type=SignalType.PULLBACK, direction=Direction.LONG, score=score, reference_price=1.085, time=T0)
    p = plan()
    risk = RiskModule(RiskConfig()).evaluate(p, snap, account(), [], T0)
    return ResearchContext(snap, [sig], p, risk)


def test_rule_based_execute_when_aligned():
    out = RuleBasedAnalyst().assess(_ctx())
    assert out.trend_aligned and out.recommendation == "execute"
    assert out.market_regime is MarketRegime.TRENDING
    assert out.confidence_score == 4 and out.strength == "strong" and out.stars == "★★★★☆"
    assert out.max_loss_within_plan and out.source == "rules"


def test_rule_based_counter_trend_is_cautious():
    out = RuleBasedAnalyst().assess(_ctx(score=58.0, trend_h1=Trend.DOWN, trend_h4=Trend.DOWN))
    assert not out.trend_aligned
    assert out.recommendation in ("watch", "skip")
    assert any("逆勢" in c for c in out.concerns)


class _FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _client(response=None, error=None):
    msgs = _FakeMessages(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def _parsed(**kw):
    base = dict(trend_aligned=True, market_regime="trending", confidence_score=5, strength="strong", max_loss_within_plan=True, recommendation="execute", rationale="順勢拉回", concerns=[])
    base.update(kw)
    return AnalystOutput(**base)


def test_claude_analyst_request_shape_and_result():
    resp = SimpleNamespace(stop_reason="end_turn", parsed_output=_parsed(), model="claude-fable-5-1")
    client, msgs = _client(resp)
    out = ClaudeAnalyst(ResearchConfig(model="claude-fable-5-1", effort="high"), client=client).assess(_ctx())
    assert out.source == "claude:claude-fable-5-1" and out.confidence_score == 5
    kw = msgs.calls[0]
    assert kw["model"] == "claude-fable-5-1"
    assert kw["output_format"] is AnalystOutput
    assert kw["output_config"] == {"effort": "high"}
    assert "thinking" not in kw  # Fable 5.x:thinking 永遠開啟,不可傳
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "EURUSD" in kw["messages"][0]["content"]


def test_claude_analyst_no_fallbacks_when_disabled():
    resp = SimpleNamespace(stop_reason="end_turn", parsed_output=_parsed(), model="claude-opus-5-5")
    client, msgs = _client(resp)
    ClaudeAnalyst(ResearchConfig(model="claude-opus-5-5", fallbacks=False), client=client).assess(_ctx())
    assert "betas" not in msgs.calls[0] and "fallbacks" not in msgs.calls[0]


def test_claude_refusal_falls_back_to_rules():
    resp = SimpleNamespace(stop_reason="refusal", parsed_output=None, model="claude-fable-5-1", stop_details=SimpleNamespace(category="cyber"))
    client, _ = _client(resp)
    out = ClaudeAnalyst(ResearchConfig(), client=client).assess(_ctx())
    assert out.source.startswith("rules(fallback")
    assert out.concerns and "拒答" in out.concerns[0]


def test_claude_connection_error_falls_back():
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client, _ = _client(error=err)
    out = ClaudeAnalyst(ResearchConfig(), client=client).assess(_ctx())
    assert out.source.startswith("rules(fallback") and out.recommendation == "execute"


def test_claude_status_error_falls_back():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(401, request=req, json={"error": {"message": "bad key"}})
    err = anthropic.AuthenticationError("bad key", response=resp, body=None)
    client, _ = _client(error=err)
    out = ClaudeAnalyst(ResearchConfig(), client=client).assess(_ctx())
    assert "認證失敗" in out.concerns[0]
