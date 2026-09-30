from datetime import timedelta

import pytest

from fxea.approval import ApprovalGate
from fxea.config import ApprovalConfig, RiskConfig
from fxea.memo import MemoBuilder, make_memo_id, render_memo
from fxea.models import Decision, Direction, MemoStatus, Signal, SignalType, Timeframe
from fxea.research import ResearchContext, RuleBasedAnalyst
from fxea.risk import RiskModule
from fxea.state import StateStore

from .helpers import T0, account, snapshot
from .test_risk import plan


def _memo(store, risk_cfg=RiskConfig(), acct=None):
    snap = snapshot()
    sig = Signal(symbol="EURUSD", timeframe=Timeframe.H1, signal_type=SignalType.PULLBACK, direction=Direction.LONG, score=78, reference_price=1.085, time=T0)
    p = plan()
    risk = RiskModule(risk_cfg).evaluate(p, snap, acct or account(), [], T0)
    research = RuleBasedAnalyst().assess(ResearchContext(snap, [sig], p, risk))
    return MemoBuilder(store).build(snap, [sig], p, risk, research, T0)


def test_memo_id_format():
    assert make_memo_id(7, T0) == "FD-007-0302"


def test_memo_ready_and_render(tmp_path):
    store = StateStore(tmp_path)
    memo = _memo(store)
    assert memo.status is MemoStatus.READY and memo.awaiting_human
    assert memo.memo_id == "FD-001-0302"
    text = render_memo(memo)
    for needle in ["最終決策備忘錄", "FD-001-0302", "1 設定摘要", "2 訊號強度", "3 風險等級", "4 交易計畫", "5 最終狀態", "可進行決策", "需要人工審核", "fxea decide"]:
        assert needle in text
    assert "★" in text


def test_memo_blocked_when_risk_fails(tmp_path):
    store = StateStore(tmp_path)
    memo = _memo(store, acct=account(equity=8900.0, peak=10000.0))
    assert memo.status is MemoStatus.BLOCKED and not memo.awaiting_human
    assert "風險阻擋" in render_memo(memo)
    assert memo.note and "回撤" in memo.note


def test_human_approval_flow(tmp_path):
    store = StateStore(tmp_path)
    gate = ApprovalGate(store, ApprovalConfig(mode="human"))
    memo = gate.submit(_memo(store), T0)
    assert memo.decision is Decision.PENDING
    assert [m.memo_id for m in gate.pending()] == [memo.memo_id]
    assert gate.active_symbols() == {"EURUSD"}

    gate.decide(memo.memo_id, "watchlist", by="alice")
    assert gate.pending() == [] and [m.memo_id for m in gate.watchlist()] == [memo.memo_id]
    gate.decide(memo.memo_id, Decision.APPROVE, by="alice")
    assert [m.memo_id for m in gate.approved_unexecuted()] == [memo.memo_id]
    gate.decide(memo.memo_id, Decision.REJECT)  # 執行前撤回
    with pytest.raises(ValueError):
        gate.decide(memo.memo_id, Decision.APPROVE)
    assert gate.active_symbols() == set()


def test_cannot_decide_blocked_memo(tmp_path):
    store = StateStore(tmp_path)
    gate = ApprovalGate(store, ApprovalConfig())
    memo = gate.submit(_memo(store, acct=account(equity=8900.0, peak=10000.0)), T0)
    with pytest.raises(ValueError):
        gate.decide(memo.memo_id, Decision.APPROVE)


def test_auto_mode_and_expiry(tmp_path):
    store = StateStore(tmp_path)
    auto = ApprovalGate(store, ApprovalConfig(mode="auto"))
    memo = auto.submit(_memo(store), T0)
    assert memo.decision is Decision.APPROVE and memo.decided_by == "auto"

    human = ApprovalGate(store, ApprovalConfig(mode="human", pending_ttl_minutes=60))
    m2 = human.submit(_memo(store), T0)
    assert human.expire_stale(T0 + timedelta(minutes=30)) == []
    expired = human.expire_stale(T0 + timedelta(minutes=61))
    assert [m.memo_id for m in expired] == [m2.memo_id]
    assert store.load_memo(m2.memo_id).status is MemoStatus.EXPIRED
    assert "已失效" in render_memo(store.load_memo(m2.memo_id))
