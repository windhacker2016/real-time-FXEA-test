"""圖 7 最終決策備忘錄:設定摘要、訊號強度、風險等級、交易計畫、最終狀態。

「最後,我會得到一個明確決策:執行、觀察,或略過。」
"""
from __future__ import annotations

import unicodedata
from datetime import datetime

from .models import (
    DecisionMemo,
    Decision,
    MarketSnapshot,
    MemoStatus,
    ResearchAssessment,
    RiskReport,
    Signal,
    TradePlan,
)
from .state import StateStore


def make_memo_id(seq: int, when: datetime) -> str:
    return f"FD-{seq:03d}-{when:%m%d}"


class MemoBuilder:
    def __init__(self, store: StateStore):
        self.store = store

    def build(
        self,
        snapshot: MarketSnapshot,
        signals: list[Signal],
        plan: TradePlan,
        risk: RiskReport,
        research: ResearchAssessment,
        now: datetime,
    ) -> DecisionMemo:
        seq = self.store.next_seq("memo")
        status = MemoStatus.READY if (risk.passed and plan.valid) else MemoStatus.BLOCKED
        note = None
        if status is MemoStatus.BLOCKED:
            note = "風險模組阻擋:" + ("; ".join(risk.blocked_reasons) or plan.invalid_reason or "")
        return DecisionMemo(
            memo_id=make_memo_id(seq, now),
            created_at=now,
            symbol=snapshot.symbol,
            snapshot=snapshot,
            signals=signals,
            plan=plan,
            risk=risk,
            research=research,
            status=status,
            note=note,
        )


# ---------------------------------------------------------------------------
# 文字渲染
# ---------------------------------------------------------------------------


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _w(s))


def render_memo(memo: DecisionMemo, inner: int = 62) -> str:
    L: list[str] = []
    top = "╔" + "═" * (inner + 2) + "╗"
    mid = "╟" + "─" * (inner + 2) + "╢"
    bot = "╚" + "═" * (inner + 2) + "╝"

    def row(label: str, value: str = "") -> None:
        body = _pad(label, 14) + value if label else " " * 14 + value
        L.append("║ " + _pad(body, inner) + " ║")

    def header(left: str, right: str) -> None:
        L.append("║ " + _pad(left, inner - _w(right)) + right + " ║")

    plan, risk, r = memo.plan, memo.risk, memo.research
    status_zh = {
        MemoStatus.READY: "可進行決策",
        MemoStatus.BLOCKED: "風險阻擋 — 不可交易",
        MemoStatus.EXPIRED: "已失效",
    }[memo.status]

    L.append(top)
    header("最終決策備忘錄", f"ID: {memo.memo_id}")
    header(f"{memo.symbol}  {plan.direction.label_zh}  {plan.signal_type.label_zh}", f"日期: {memo.created_at:%d/%m/%Y %H:%M} UTC")
    L.append(mid)
    row("1 設定摘要", f"趨勢一致:{'是' if r.trend_aligned else '否'}")
    row("", f"市況:{r.market_regime.label_zh}")
    row("", f"時間週期:{memo.timeframes_zh}")
    L.append(mid)
    row("2 訊號強度", f"信心分數:{r.stars}")
    row("", f"強度:{r.strength_zh}(訊號評分 {memo.primary_signal.score:.0f})")
    L.append(mid)
    row("3 風險等級", f"風險評級:{risk.rating.label_zh}")
    row("", f"最大虧損符合計畫:{'是' if r.max_loss_within_plan else '否'}")
    row("", f"部位:{risk.lots:.2f} 手,風險 {risk.risk_amount:.2f}({risk.risk_pct:.2f}%)")
    L.append(mid)
    row("4 交易計畫", f"進場:{plan.entry_price}   進場區:{plan.entry_zone_low} – {plan.entry_zone_high}")
    row("", f"停損:{plan.stop_loss}   停利:{plan.take_profit}")
    row("", f"風險報酬比:1:{plan.risk_reward:.2f}   信心:{plan.confidence.label_zh}")
    row("", f"失效:{plan.invalidation_level}")
    L.append(mid)
    row("5 最終狀態", status_zh)
    if memo.decision is not Decision.PENDING:
        by = memo.decided_by or "-"
        row("", f"決策:{memo.decision.label_zh}(by {by})")
    L.append(bot)

    for c in risk.checks:
        L.append(f"  {'✔' if c.passed else '✘'} {c.label_zh}:{c.detail}")
    L.append(f"  研究({r.source}):{r.rationale}")
    for concern in r.concerns:
        L.append(f"    ! {concern}")
    L.append(f"  失效條件:{plan.invalidation_condition}")
    if memo.note:
        L.append(f"  備註:{memo.note}")

    if memo.awaiting_human:
        L.append("")
        L.append("  ─── 需要人工審核 ───")
        L.append("  [核准] 執行交易     [觀察清單] 監控條件     [拒絕] 不要交易")
        L.append(f"  fxea decide {memo.memo_id} approve | watchlist | reject")
    L.append("")
    L.append("  決策引擎啟用 · " + ("最終檢查完成,所有檢查皆通過" if risk.passed else "最終檢查未通過") + " · 由你做最後決定")
    return "\n".join(L)
