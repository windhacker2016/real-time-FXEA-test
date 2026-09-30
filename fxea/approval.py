"""圖 7 下半部:需要人工審核 → 核准(執行交易)/ 觀察清單(監控條件)/ 拒絕(不要交易)。

``approval.mode = human``(預設)時,備忘錄寫入待審佇列,等待 ``fxea decide``;
``auto`` 只建議用於紙上交易 / 回放。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import ApprovalConfig
from .models import Decision, DecisionMemo, MemoStatus, utcnow
from .state import StateStore

_ALLOWED = {
    Decision.PENDING: {Decision.APPROVE, Decision.WATCHLIST, Decision.REJECT},
    Decision.WATCHLIST: {Decision.APPROVE, Decision.REJECT},
    Decision.APPROVE: {Decision.REJECT},  # 尚未執行前可以撤回
    Decision.REJECT: set(),
}


class ApprovalGate:
    def __init__(self, store: StateStore, cfg: ApprovalConfig):
        self.store = store
        self.cfg = cfg

    def submit(self, memo: DecisionMemo, now: datetime | None = None) -> DecisionMemo:
        now = now or utcnow()
        if self.cfg.mode == "auto" and memo.status is MemoStatus.READY:
            memo.decision = Decision.APPROVE
            memo.decided_by = "auto"
            memo.decided_at = now
        self.store.save_memo(memo)
        self.store.journal({"type": "memo", "memo_id": memo.memo_id, "symbol": memo.symbol, "status": memo.status.value, "decision": memo.decision.value})
        return memo

    def decide(
        self,
        memo_id: str,
        decision: Decision | str,
        by: str = "human",
        note: str | None = None,
        now: datetime | None = None,
    ) -> DecisionMemo:
        decision = Decision(decision)
        memo = self.store.load_memo(memo_id)
        if memo.status is not MemoStatus.READY:
            raise ValueError(f"{memo_id} 狀態為「{memo.status.value}」,不可決策")
        if memo.executed:
            raise ValueError(f"{memo_id} 已執行,不可變更")
        if decision not in _ALLOWED[memo.decision]:
            raise ValueError(f"{memo_id} 目前為「{memo.decision.label_zh}」,不可改為「{decision.label_zh}」")
        memo.decision = decision
        memo.decided_by = by
        memo.decided_at = now or utcnow()
        if note:
            memo.note = note
        self.store.save_memo(memo)
        self.store.journal({"type": "decision", "memo_id": memo_id, "decision": decision.value, "by": by})
        return memo

    # ---- 查詢 ----------------------------------------------------------
    def pending(self) -> list[DecisionMemo]:
        return [m for m in self.store.list_memos() if m.awaiting_human]

    def watchlist(self) -> list[DecisionMemo]:
        return [
            m
            for m in self.store.list_memos()
            if m.status is MemoStatus.READY and m.decision is Decision.WATCHLIST and not m.executed
        ]

    def approved_unexecuted(self) -> list[DecisionMemo]:
        return [
            m
            for m in self.store.list_memos()
            if m.status is MemoStatus.READY and m.decision is Decision.APPROVE and not m.executed
        ]

    def active_symbols(self) -> set[str]:
        """有待審 / 觀察中 / 已核准未執行備忘錄的商品,避免重複產生。"""
        return {m.symbol for m in self.pending() + self.watchlist() + self.approved_unexecuted()}

    # ---- 失效 ----------------------------------------------------------
    def expire(self, memo: DecisionMemo, reason: str, now: datetime | None = None) -> DecisionMemo:
        memo.status = MemoStatus.EXPIRED
        memo.note = reason
        memo.decided_at = memo.decided_at or (now or utcnow())
        self.store.save_memo(memo)
        self.store.journal({"type": "expire", "memo_id": memo.memo_id, "reason": reason})
        return memo

    def expire_stale(self, now: datetime | None = None) -> list[DecisionMemo]:
        now = now or utcnow()
        ttl = timedelta(minutes=self.cfg.pending_ttl_minutes)
        expired = []
        for memo in self.pending():
            if now - memo.created_at > ttl:
                expired.append(self.expire(memo, f"超過 {self.cfg.pending_ttl_minutes} 分鐘未審核,自動失效", now))
        return expired
