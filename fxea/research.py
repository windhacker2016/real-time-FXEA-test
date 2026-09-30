"""圖 1「研究」:AI 交易員的分析層。

- :class:`ClaudeAnalyst`:用 Claude(預設 ``claude-fable-5-1``)審視候選交易設定,
  以結構化輸出回填備忘錄第 1–3 區塊(趨勢一致、市況、信心、強度、風險判讀)。
- :class:`RuleBasedAnalyst`:離線 / 無金鑰 / API 失敗時的確定性備援。

Claude 只提供評估,不下單;最終決定永遠由風險模組 + 人工核准把關。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import ResearchConfig
from .models import (
    AnalystOutput,
    MarketRegime,
    MarketSnapshot,
    Position,
    ResearchAssessment,
    RiskReport,
    Signal,
    TradePlan,
    Trend,
)

log = logging.getLogger("fxea.research")


@dataclass
class ResearchContext:
    snapshot: MarketSnapshot
    signals: list[Signal]
    plan: TradePlan
    risk: RiskReport
    open_positions: list[Position] = field(default_factory=list)


class Analyst(Protocol):
    name: str

    def assess(self, ctx: ResearchContext) -> ResearchAssessment: ...


# ---------------------------------------------------------------------------
# 規則式分析器(確定性備援)
# ---------------------------------------------------------------------------


class RuleBasedAnalyst:
    name = "rules"

    def assess(self, ctx: ResearchContext) -> ResearchAssessment:
        snap, plan, risk = ctx.snapshot, ctx.plan, ctx.risk
        sig = ctx.signals[0]
        d = plan.direction

        h1_ok = snap.trend_h1.matches(d)
        h4_ok = snap.trend_h4.matches(d)
        aligned = h1_ok and h4_ok
        partial = h1_ok or h4_ok

        if snap.atr_percentile >= 85:
            regime = MarketRegime.VOLATILE
        elif snap.trend_h1 is not Trend.SIDEWAYS and snap.trend_h4 is not Trend.SIDEWAYS:
            regime = MarketRegime.TRENDING
        else:
            regime = MarketRegime.RANGING

        if sig.score >= 85:
            conf = 5
        elif sig.score >= 75:
            conf = 4
        elif sig.score >= 65:
            conf = 3
        elif sig.score >= 55:
            conf = 2
        else:
            conf = 1
        if not aligned:
            conf -= 1
        if not risk.passed:
            conf -= 1
        conf = max(1, min(5, conf))
        strength = "strong" if conf >= 4 else ("moderate" if conf == 3 else "weak")

        max_loss_ok = risk.check("max_loss").passed and risk.check("position_size").passed

        concerns: list[str] = []
        for c in risk.checks:
            if not c.passed:
                concerns.append(f"{c.label_zh}未通過:{c.detail}")
        if not partial:
            concerns.append("H1 與 H4 趨勢皆未與交易方向一致(逆勢)")
        elif not aligned:
            concerns.append("僅單一週期趨勢同向")
        if regime is MarketRegime.VOLATILE:
            concerns.append(f"波動偏高(ATR 百分位 {snap.atr_percentile:.0f})")
        if snap.news_risk:
            concerns.append("視窗內有高影響新聞")
        if not plan.valid:
            concerns.append(f"交易計畫無效:{plan.invalid_reason}")

        if risk.passed and plan.valid and conf >= 3 and partial:
            rec = "execute"
        elif plan.valid and conf >= 2:
            rec = "watch"
        else:
            rec = "skip"

        rationale = (
            f"{sig.signal_type.label_zh}訊號({sig.timeframe.value},評分 {sig.score:.0f})"
            f"{'順' if partial else '逆'}勢{d.label_zh};市況{regime.label_zh},"
            f"R:R 1:{plan.risk_reward:.2f},風險模組{'通過' if risk.passed else '阻擋'}。"
        )
        return ResearchAssessment(
            trend_aligned=aligned,
            market_regime=regime,
            confidence_score=conf,  # type: ignore[arg-type]
            strength=strength,  # type: ignore[arg-type]
            max_loss_within_plan=max_loss_ok,
            recommendation=rec,  # type: ignore[arg-type]
            rationale=rationale,
            concerns=concerns,
            source="rules",
        )


# ---------------------------------------------------------------------------
# Claude 分析器
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一位全天候 AI 外匯交易員的研究分析師,負責在系統送出「最終決策備忘錄」給人工審核之前,做最後一輪獨立審視。

你會收到一份 JSON:市場快照(價格、成交量、H1/H4 趨勢、ATR、RSI、關鍵價位、新聞)、訊號引擎標記的訊號、交易計畫(進場區、停損、目標、失效條件、R:R)、以及風險模組五項檢查的結果。

請以交易員的角度判斷這個設定是否值得執行,並依指定結構回覆:
- trend_aligned:交易方向是否與 H1 與 H4 趨勢一致
- market_regime:trending / ranging / volatile
- confidence_score:1–5 的整體信心
- strength:訊號強度 weak / moderate / strong
- max_loss_within_plan:本筆最大虧損是否在計畫與風險預算之內
- recommendation:execute(執行)/ watch(先觀察)/ skip(略過)
- rationale:用繁體中文簡潔說明理由
- concerns:具體疑慮清單(沒有就空清單)

原則:風險模組任何一項未通過就不應建議 execute;逆勢或新聞衝突時保守;不要編造資料中沒有的資訊。你只提供評估,不會下單,最後決定由人工做出。"""


class ClaudeAnalyst:
    name = "claude"

    def __init__(self, cfg: ResearchConfig, client: Any | None = None, fallback: Analyst | None = None):
        self.cfg = cfg
        self.fallback: Analyst = fallback or RuleBasedAnalyst()
        if client is None:
            import anthropic

            client = anthropic.Anthropic(timeout=cfg.timeout_seconds)
        self.client = client

    # ------------------------------------------------------------------
    @staticmethod
    def build_payload(ctx: ResearchContext) -> str:
        data = {
            "snapshot": ctx.snapshot.model_dump(mode="json"),
            "signals": [s.model_dump(mode="json") for s in ctx.signals],
            "plan": ctx.plan.model_dump(mode="json"),
            "risk": ctx.risk.model_dump(mode="json"),
            "open_positions": [
                {"symbol": p.symbol, "direction": p.direction.value, "lots": p.lots, "risk_amount": p.risk_amount}
                for p in ctx.open_positions
                if p.status == "open"
            ],
        }
        return "請評估以下候選交易設定:\n\n" + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1)

    def _use_fallback(self, ctx: ResearchContext, why: str) -> ResearchAssessment:
        log.warning("Claude 分析改用規則式備援:%s", why)
        out = self.fallback.assess(ctx)
        out.concerns = [f"Claude 分析未完成({why}),以下為規則式評估"] + list(out.concerns)
        out.source = f"rules(fallback:{self.name})"
        return out

    def assess(self, ctx: ResearchContext) -> ResearchAssessment:
        import anthropic

        kwargs: dict[str, Any] = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": self.build_payload(ctx)}],
            output_format=AnalystOutput,
            output_config={"effort": self.cfg.effort},
        )
        if self.cfg.fallbacks:
            # 伺服器端拒答備援:安全分類器拒答時,同一請求改由備援模型完成
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"

        try:
            resp = self.client.beta.messages.parse(**kwargs)
        except anthropic.AuthenticationError as exc:
            return self._use_fallback(ctx, f"認證失敗 {exc}")
        except anthropic.RateLimitError as exc:
            return self._use_fallback(ctx, f"速率限制 {exc}")
        except anthropic.APIStatusError as exc:
            return self._use_fallback(ctx, f"API 錯誤 {exc.status_code} {exc}")
        except anthropic.APIConnectionError as exc:
            return self._use_fallback(ctx, f"連線失敗 {exc}")

        if getattr(resp, "stop_reason", None) == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return self._use_fallback(ctx, f"模型拒答 category={category}")

        parsed = getattr(resp, "parsed_output", None)
        if parsed is None:
            return self._use_fallback(ctx, f"無結構化輸出 stop_reason={getattr(resp, 'stop_reason', None)}")

        served_by = getattr(resp, "model", self.cfg.model)
        return ResearchAssessment(**parsed.model_dump(), source=f"claude:{served_by}")
