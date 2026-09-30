"""圖 5 風險模組:部位大小、曝險上限、回撤控制、波動檢查、最大虧損。

五項全部通過 → 繼續;任何一項未通過 → 阻擋。每次決策都在保護資金。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import RiskConfig
from .instruments import get_instrument
from .models import Account, MarketSnapshot, Position, RiskCheck, RiskRating, RiskReport, TradePlan


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
    ) -> RiskReport:
        cfg = self.cfg
        inst = get_instrument(plan.symbol)
        ccy = account.currency
        equity = max(account.equity, 0.0)
        checks: list[RiskCheck] = []

        # 1 部位大小:依每筆交易風險計算部位 ------------------------------
        stop_pips = inst.pips(plan.risk_distance)
        pip_value = inst.pip_value(plan.entry_price, ccy)
        budget = equity * cfg.risk_per_trade_pct / 100.0
        raw_lots = budget / (stop_pips * pip_value) if stop_pips > 0 and pip_value > 0 else 0.0
        lots = inst.round_lots(raw_lots)
        risk_amount = lots * stop_pips * pip_value
        risk_pct = risk_amount / equity * 100.0 if equity > 0 else 0.0
        size_ok = lots >= inst.lot_min and plan.valid
        detail = (
            f"停損 {stop_pips:.1f} 點 × 每點 {pip_value:.2f} {ccy};"
            f"預算 {budget:.2f} → {lots:.2f} 手,風險金額 {risk_amount:.2f} {ccy}({risk_pct:.2f}%)"
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
        dd = account.drawdown_pct
        checks.append(
            RiskCheck(
                name="drawdown",
                label_zh="回撤檢查",
                passed=dd < cfg.max_drawdown_pct,
                detail=f"目前回撤 {dd:.2f}%(門檻 {cfg.max_drawdown_pct}%)",
                value=round(dd, 3),
                limit=cfg.max_drawdown_pct,
            )
        )

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
        high_flags = pct > 85 or dd > cfg.max_drawdown_pct / 2 or snapshot.news_risk
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
