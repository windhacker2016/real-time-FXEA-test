import json

from fxea.alerts import MemoryAlertSink
from fxea.app import build_loop
from fxea.config import load_settings
from fxea.models import AlertLevel, Decision, MemoStatus


def _settings(tmp_path, **over):
    base = {
        "watchlist": ["EURUSD", "GBPUSD", "USDJPY"],
        "state_dir": str(tmp_path / "state"),
        "approval": {"mode": "auto"},
        "alerts": {"console": False, "file": None},
        "monitor": {"status_every": 5, "proactive_review_every": 7},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return load_settings(None, base)


def test_auto_mode_end_to_end(tmp_path):
    sink = MemoryAlertSink()
    loop = build_loop(_settings(tmp_path), alerts=sink)
    reports = loop.run(cycles=80, interval=0)
    assert len(reports) == 80 and reports[-1].cycle == 80

    memos = loop.state.list_memos()
    ready = [m for m in memos if m.status is MemoStatus.READY]
    assert memos and ready
    assert all(m.decision is Decision.APPROVE and m.decided_by == "auto" for m in ready)
    executed = [m for m in memos if m.executed]
    assert executed and all(m.position_id for m in executed)
    assert loop.broker.all_positions()

    titles = {a.title for a in sink.alerts}
    assert "系統全天候上線" in titles and "已執行交易" in titles and "狀態監控" in titles
    assert (tmp_path / "state" / "journal.jsonl").exists()
    status = json.loads((tmp_path / "state" / "status.json").read_text())
    assert status["system"] == "online" and status["cycle"] == 80
    # 每份 memo 都有五項風險檢查與研究結果
    for m in memos:
        assert len(m.risk.checks) == 5 and m.research.rationale


def test_drawdown_breaker_halts_loudly_and_resumes(tmp_path):
    sink = MemoryAlertSink()
    loop = build_loop(_settings(tmp_path, risk={"max_drawdown_pct": 0.0, "drawdown_cooldown_hours": 0}), alerts=sink)
    loop.run(cycles=20, interval=0)
    assert loop.governor.halted
    crit = [a for a in sink.alerts if a.level is AlertLevel.CRITICAL]
    assert len(crit) == 1 and crit[0].title.startswith("交易暫停") and "fxea resume" in crit[0].message
    assert loop.state.list_memos() == []  # 暫停中不產生新設定,也不洗版
    status = json.loads((tmp_path / "state" / "status.json").read_text())
    assert status["system"] == "halted" and status["risk_mode"] == "halted" and status["halt_reason"]
    assert (tmp_path / "state" / "risk_state.json").exists()

    # 人工恢復(先把上限調回合理值)後,設定重新產生
    loop.settings.risk.max_drawdown_pct = 50.0
    loop.governor.resume(loop.feed.now(), by="tester")
    loop.run(cycles=60, interval=0)
    assert not loop.governor.halted
    assert loop.state.list_memos()
    assert json.loads((tmp_path / "state" / "status.json").read_text())["system"] == "online"


def test_block_cooldown_suppresses_repeats(tmp_path):
    loop = build_loop(_settings(tmp_path, risk={"volatility_percentile_band": [200, 300]}), alerts=MemoryAlertSink())
    loop.run(cycles=30, interval=0)
    memos = loop.state.list_memos()
    assert memos and all(m.status is MemoStatus.BLOCKED for m in memos)
    # 同商品、同一組失敗原因:兩份備忘錄之間至少隔 240 分鐘(每輪 = 1 小時)
    groups: dict[tuple, list] = {}
    for m in memos:
        key = (m.symbol, tuple(c.name for c in m.risk.checks if not c.passed))
        groups.setdefault(key, []).append(m.created_at)
    for times in groups.values():
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
        assert all(g >= 240 for g in gaps), gaps
    assert len(memos) < 30 * 3  # 遠少於「每輪每商品一份」


def test_human_mode_waits_then_executes_after_approval(tmp_path):
    sink = MemoryAlertSink()
    loop = build_loop(_settings(tmp_path, approval={"mode": "human"}), alerts=sink)
    pending = []
    for _ in range(80):
        loop.run_cycle()
        pending = loop.gate.pending()
        if pending:
            break
    assert pending, "應該產生等待人工審核的備忘錄"
    assert loop.broker.open_positions() == []
    memo = pending[0]
    assert any(a.title.startswith("交易計畫完成 — 需要人工核准") for a in sink.alerts)

    loop.gate.decide(memo.memo_id, Decision.APPROVE, by="tester")
    loop.run_cycle()
    memo = loop.state.load_memo(memo.memo_id)
    assert memo.executed or memo.status is MemoStatus.EXPIRED
    if memo.executed:
        assert memo.position_id and loop.broker.open_positions()[0].memo_id == memo.memo_id
        # 同商品有部位時不再產生新備忘錄
        before = len(loop.state.list_memos())
        loop.run_cycle()
        assert all(m.symbol != memo.symbol or m.memo_id == memo.memo_id for m in loop.state.list_memos()[before:])


def test_cycle_survives_scanner_error(tmp_path):
    sink = MemoryAlertSink()
    loop = build_loop(_settings(tmp_path, watchlist=["EURUSD"]), alerts=sink)
    loop.watchlist.append("BADSYM")
    loop.base_watchlist.append("BADSYM")
    loop.feed.candles = lambda symbol, tf, n: (_ for _ in ()).throw(RuntimeError("feed down")) if symbol == "BADSYM" else type(loop.feed).candles(loop.feed, symbol, tf, n)
    report = loop.run_cycle()
    assert report.scanned == 1
    assert any(a.title == "掃描失敗" and a.symbol == "BADSYM" for a in sink.alerts)
