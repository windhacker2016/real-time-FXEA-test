"""圖 5 風險模組:部位大小、曝險上限、回撤控制、波動檢查、最大虧損。

五項全部通過 → 繼續;任何一項未通過 → 阻擋。每次決策都在保護資金。

:class:`RiskGovernor` 是回撤斷路器的狀態機:觸及 ``max_drawdown_pct`` 時進入「暫停」
(不再開新倉、發 CRITICAL 警示),冷卻期滿或人工 ``fxea resume`` 後進入「恢復期」
(風險縮減、從目前權益重新起算回撤),權益回到暫停前高點才回復「正常」。
沒有這個狀態機,回撤檢查一旦失敗就會永久鎖死:沒有部位 → 權益不變 → 永遠通不過檢查。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import RiskConfig
from .instruments import get_instrument
from .models import (
    Account,
    MarketSnapshot,
    Position,
    RiskCheck,
    RiskMode,
    RiskRating,
    RiskReport,
    RiskState,
    TradePlan,
)
from .state import StateStore


class RiskModule:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def evaluate(
        self,
        plan: TradePlan,
        snapshot: MarketSnapshot,
        account: Account,
        open_positions: list[Position],
        now: datetime,
        *,
        risk_scale: float = 1.0,
        drawdown_pct: float | None = None,
        mode: RiskMode = RiskMode.NORMAL,
    ) -> RiskReport:
        cfg = self.cfg
        inst = get_instrument(plan.symbol)
        ccy = account.currency
        equity = max(account.equity, 0.0)
        checks: list[RiskCheck] = []

        # 1 部位大小:依每筆交易風險計算部位 ------------------------------
        stop_pips = inst.pips(plan.risk_distance)
        pip_value = inst.pip_value(plan.entry_price, ccy)
        budget = equity * cfg.risk_per_trade_pct / 100.0 * risk_scale
        raw_lots = budget / (stop_pips * pip_value) if stop_pips > 0 and pip_value > 0 else 0.0
        lots = inst.round_lots(raw_lots)
        risk_amount = lots * stop_pips * pip_value
        risk_pct = risk_amount / equity * 100.0 if equity > 0 else 0.0
        size_ok = lots >= inst.lot_min and plan.valid
        scale_note = f"(風險 ×{risk_scale:g})" if risk_scale != 1.0 else ""
        detail = (
            f"停損 {stop_pips:.1f} 點 × 每點 {pip_value:.2f} {ccy};"
            f"預算 {budget:.2f}{scale_note} → {lots:.2f} 手,風險金額 {risk_amount:.2f} {ccy}({risk_pct:.2f}%)"
        )
        if not plan.valid:
            detail = f"交易計畫無效:{plan.invalid_reason}"
        elif lots < inst.lot_min:
            detail += f";低於最小手數 {inst.lot_min}"
        checks.append(RiskCheck(name="position_size", label_zh="部位大小檢查", passed=size_ok, detail=detail, value=lots, limit=inst.lot_min))

        # 2 曝險上限:檢查所有部位的總曝險 --------------------------------
        open_risk = sum(p.risk_amount for p in open_positions if p.status == "open")
        total_pct = (open_risk + risk_amount) / equity * 100.0 if equity > 0 else 0.0
        n_open = sum(1 for p in open_positions if p.status == "open")
        same_symbol = any(p.symbol == plan.symbol and p.status == "open" for p in open_positions)
        reasons = []
        if total_pct > cfg.max_total_exposure_pct:
            reasons.append(f"總曝險 {total_pct:.2f}% 超過上限 {cfg.max_total_exposure_pct}%")
        if n_open >= cfg.max_open_positions:
            reasons.append(f"未平倉 {n_open} 筆已達上限 {cfg.max_open_positions}")
        if same_symbol and not cfg.allow_same_symbol:
            reasons.append(f"{plan.symbol} 已有未平倉部位")
        checks.append(
            RiskCheck(
                name="exposure",
                label_zh="曝險上限檢查",
                passed=not reasons,
                detail="; ".join(reasons) if reasons else f"新增後總曝險 {total_pct:.2f}%(上限 {cfg.max_total_exposure_pct}%),未平倉 {n_open}/{cfg.max_open_positions}",
                value=round(total_pct, 3),
                limit=cfg.max_total_exposure_pct,
            )
        )

        # 3 回撤控制:確認未超過允許回撤門檻 ------------------------------
        dd = account.drawdown_pct if drawdown_pct is None else drawdown_pct
        dd_ok = dd < cfg.max_drawdown_pct and mode is not RiskMode.HALTED
        dd_detail = f"目前回撤 {dd:.2f}%(門檻 {cfg.max_drawdown_pct}%)"
        if mode is RiskMode.HALTED:
            dd_detail = "交易暫停中(回撤斷路器)— " + dd_detail
        elif mode is RiskMode.RECOVERY:
            dd_detail += f";恢復期,風險 ×{cfg.recovery_risk_scale:g}"
        checks.append(RiskCheck(name="drawdown", label_zh="回撤檢查", passed=dd_ok, detail=dd_detail, value=round(dd, 3), limit=cfg.max_drawdown_pct))

        # 4 波動檢查:確認市場波動可接受 ----------------------------------
        lo, hi = cfg.volatility_percentile_band
        pct = snapshot.atr_percentile
        vol_reasons = []
        if not (lo <= pct <= hi):
            vol_reasons.append(f"ATR 百分位 {pct:.0f} 超出可接受區間 {lo:.0f}–{hi:.0f}")
        blackout = timedelta(minutes=cfg.news_blackout_minutes)
        for e in snapshot.upcoming_news:
            if e.impact == "high" and abs(e.time - now) <= blackout:
                vol_reasons.append(f"高影響新聞 {e.currency}「{e.title}」在 {cfg.news_blackout_minutes} 分鐘內")
                break
        checks.append(
            RiskCheck(
                name="volatility",
                label_zh="波動檢查",
                passed=not vol_reasons,
                detail="; ".join(vol_reasons) if vol_reasons else f"ATR 百分位 {pct:.0f},無高影響新聞衝突",
                value=pct,
                limit=hi,
            )
        )

        # 5 最大虧損:確保不會突破最大虧損上限 ----------------------------
        # 最壞情況 = 今日已實現虧損 + 所有未平倉部位同時停損 + 本筆停損
        daily_loss = max(0.0, -account.daily_pnl)
        projected = daily_loss + open_risk + risk_amount
        daily_limit = account.balance * cfg.max_daily_loss_pct / 100.0
        loss_reasons = []
        if projected > daily_limit:
            loss_reasons.append(
                f"今日已虧 {daily_loss:.2f} + 未平倉風險 {open_risk:.2f} + 本筆 {risk_amount:.2f} = {projected:.2f} 超過日上限 {daily_limit:.2f}"
            )
        if risk_amount > budget * 1.05 + 1e-9:
            loss_reasons.append(f"本筆風險 {risk_amount:.2f} 超過單筆預算 {budget:.2f}")
        checks.append(
            RiskCheck(
                name="max_loss",
                label_zh="最大虧損檢查",
                passed=not loss_reasons,
                detail="; ".join(loss_reasons)
                if loss_reasons
                else f"今日虧損 {daily_loss:.2f} + 未平倉風險 {open_risk:.2f} + 本筆 {risk_amount:.2f} ≤ 日上限 {daily_limit:.2f}",
                value=round(projected, 2),
                limit=round(daily_limit, 2),
            )
        )

        passed = all(c.passed for c in checks)
        blocked = [f"{c.label_zh}:{c.detail}" for c in checks if not c.passed]

        # 風險評級 --------------------------------------------------------
        high_flags = pct > 85 or dd > cfg.max_drawdown_pct / 2 or snapshot.news_risk or mode is not RiskMode.NORMAL
        low_flags = pct <= 50 and plan.risk_reward >= 2.0 and dd < 2.0 and n_open == 0
        rating = RiskRating.HIGH if high_flags else (RiskRating.LOW if low_flags else RiskRating.MEDIUM)

        return RiskReport(
            checks=checks,
            passed=passed,
            lots=lots,
            risk_amount=round(risk_amount, 2),
            risk_pct=round(risk_pct, 3),
            rating=rating,
            blocked_reasons=blocked,
        )


# ---------------------------------------------------------------------------
# 回撤斷路器
# ---------------------------------------------------------------------------


@dataclass
class RiskTransition:
    time: datetime
    from_mode: RiskMode
    to_mode: RiskMode
    reason: str
    by: str = "auto"


class RiskGovernor:
    FILE = "risk_state.json"

    def __init__(self, cfg: RiskConfig, store: StateStore):
        self.cfg = cfg
        self.store = store
        raw = store.read_json(self.FILE)
        self.state = RiskState.model_validate(raw) if raw else RiskState()

    def save(self) -> None:
        self.store.write_json(self.FILE, self.state.model_dump(mode="json"))

    @property
    def mode(self) -> RiskMode:
        return self.state.mode

    @property
    def halted(self) -> bool:
        return self.state.mode is RiskMode.HALTED

    @property
    def risk_scale(self) -> float:
        if self.state.mode is RiskMode.HALTED:
            return 0.0
        if self.state.mode is RiskMode.RECOVERY:
            return self.cfg.recovery_risk_scale
        return 1.0

    def drawdown_pct(self, equity: float | None = None) -> float:
        eq = self.state.last_equity if equity is None else equity
        peak = self.state.reference_peak
        if eq is None or not peak or peak <= 0:
            return 0.0
        return max(0.0, (peak - eq) / peak * 100.0)

    def update(self, account: Account, now: datetime) -> list[RiskTransition]:
        """每輪呼叫一次;回傳這一輪發生的狀態轉換(給監控循環發警示)。"""
        cfg, st = self.cfg, self.state
        eq = account.equity
        out: list[RiskTransition] = []
        st.last_equity = eq
        if st.reference_peak is None:
            st.reference_peak = max(account.peak_equity, eq)

        if st.mode is RiskMode.HALTED:
            if cfg.drawdown_cooldown_hours > 0 and st.halted_at is not None and now - st.halted_at >= timedelta(hours=cfg.drawdown_cooldown_hours):
                out.append(self._resume(now, "auto", f"冷卻 {cfg.drawdown_cooldown_hours} 小時結束", full_risk=False))
            self.save()
            return out

        st.reference_peak = max(st.reference_peak, eq)
        if st.mode is RiskMode.RECOVERY and st.peak_at_halt is not None and eq >= st.peak_at_halt:
            out.append(RiskTransition(now, RiskMode.RECOVERY, RiskMode.NORMAL, f"權益 {eq:.2f} 回到暫停前高點 {st.peak_at_halt:.2f},恢復全額風險"))
            st.mode = RiskMode.NORMAL
            st.peak_at_halt = None

        dd = self.drawdown_pct(eq)
        if dd >= cfg.max_drawdown_pct:
            reason = f"回撤 {dd:.2f}% ≥ 上限 {cfg.max_drawdown_pct:g}%(高水位 {st.reference_peak:.2f} → 權益 {eq:.2f})"
            out.append(RiskTransition(now, st.mode, RiskMode.HALTED, reason))
            st.peak_at_halt = max(st.peak_at_halt or 0.0, st.reference_peak)
            st.mode = RiskMode.HALTED
            st.halted_at = now
            st.halt_reason = reason
            st.halts += 1
        self.save()
        return out

    def resume(self, now: datetime, by: str = "human", full_risk: bool = False) -> RiskTransition:
        if self.state.mode is not RiskMode.HALTED:
            raise ValueError(f"風險模式目前是「{self.state.mode.label_zh}」,不需要 resume")
        t = self._resume(now, by, f"由 {by} 手動恢復", full_risk)
        self.save()
        return t

    def _resume(self, now: datetime, by: str, reason: str, full_risk: bool) -> RiskTransition:
        st = self.state
        new_mode = RiskMode.NORMAL if full_risk else RiskMode.RECOVERY
        if st.last_equity is not None:
            st.reference_peak = st.last_equity  # 從目前權益重新起算回撤
        if full_risk:
            st.peak_at_halt = None
            detail = ";全額風險,高水位重設為目前權益"
        else:
            target = f"{st.peak_at_halt:.2f}" if st.peak_at_halt is not None else "暫停前高點"
            detail = f";恢復期風險 ×{self.cfg.recovery_risk_scale:g},權益回到 {target} 後回復全額"
        t = RiskTransition(now, st.mode, new_mode, reason + detail, by)
        st.mode = new_mode
        st.resumed_at = now
        st.resumed_by = by
        st.halted_at = None
        return t
