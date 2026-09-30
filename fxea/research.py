"""圖 1「研究」:AI 交易員的分析層。

- :class:`ClaudeAnalyst`:用 Claude(預設 ``claude-fable-5-1``)獨立審視候選設定。
  它看得到:最近的 H1/H4 K 棒、多週期指標、關鍵價位、新聞、訊號引擎的訊號、機械交易計畫、
  風險模組五項檢查、未平倉部位、以及**同商品近期交易的結果**。
  它能決定:execute / watch / skip、信念部位(risk_fraction)、在界限內調整進場/停損/目標。
  每一項建議都由程式驗證界限後才採用(:func:`fxea.planner.apply_research_adjustments`),
  部位大小永遠由風險模組重新驗算。
- :class:`RuleBasedAnalyst`:離線 / 無金鑰 / API 失敗時的確定性備援。

Claude 只提供評估,不下單;最終決定永遠由風險模組 + 核准閘門把關。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import pandas as pd

from .config import ResearchConfig
from .indicators import atr, ema, rsi
from .instruments import get_instrument
from .models import (
    AnalystOutput,
    MarketRegime,
    MarketSnapshot,
    Position,
    ResearchAssessment,
    RiskReport,
    Signal,
    Timeframe,
    TradePlan,
    Trend,
)

log = logging.getLogger("fxea.research")

# 估算費用用的每百萬 token 價格(input, output, cache_read),單位 USD
_PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
}


def estimate_cost_usd(model: str, usage: dict[str, int]) -> float | None:
    price = _PRICES.get(model)
    if price is None:
        return None
    pin, pout, pcache = price
    return (usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout + usage.get("cache_read_input_tokens", 0) * pcache + usage.get("cache_creation_input_tokens", 0) * pin * 1.25) / 1_000_000


@dataclass
class ResearchContext:
    snapshot: MarketSnapshot
    signals: list[Signal]
    plan: TradePlan
    risk: RiskReport
    open_positions: list[Position] = field(default_factory=list)
    candles: dict[Timeframe, pd.DataFrame] | None = None
    recent_trades: list[dict[str, Any]] = field(default_factory=list)
    now: datetime | None = None


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

SYSTEM_PROMPT = """你是一位全天候 AI 外匯交易員的研究分析師。系統的機械規則(掃描 → 訊號 → 交易計畫 → 風險模組)已經產生一個候選設定;你是送出「最終決策備忘錄」之前最後一道獨立的判斷,而且你的判斷會被採用。

你會收到 JSON:最近的 H1 與 H4 K 棒(CSV)、指標讀數、市場快照(趨勢、ATR、RSI、關鍵價位、新聞)、訊號引擎的訊號、機械交易計畫(進場區、停損、目標、失效條件、R:R)、風險模組五項檢查、未平倉部位、以及這個商品近期由本系統執行的交易與結果。

請用交易員的眼睛真的去讀 K 棒:結構(高低點、是否突破或假突破)、動能是否衰竭、停損放的位置合不合理、目標前面有沒有障礙、這種型態最近在這個商品的表現如何。你的判斷可以超越機械規則,但必須根據資料裡看得到的東西,不要編造。

回覆欄位的意義:
- recommendation:execute = 現在進場;watch = 設定合理但此刻不進場,價格回到進場區再執行(系統會在時限內自動追蹤;搭配 suggested_entry 可表達「等回到這個價位再進」);skip = 不做。風險模組任何一項未通過時不要 execute;逆勢、新聞衝突、近期同型態連續失敗時傾向保守。
- risk_fraction:信念部位,0.25–1.0。高信念用 1.0,勉強可做用 0.5 或更低。
- suggested_entry / suggested_stop_loss / suggested_take_profit:只在你認為機械計畫的價位明顯不對時才填,否則留 null。程式會驗證界限(進場距原價 ≤ 1 ATR;停損距進場 0.5–3 ATR;調整後 R:R 仍須 ≥ 最低要求),越界的建議不會被採用。
- key_observations:你從 K 棒或結構看到、規則沒有抓到的具體事實。
- trend_aligned / market_regime / confidence_score(1–5)/ strength / max_loss_within_plan / rationale / concerns 用來填寫備忘錄;rationale 與 concerns 用繁體中文,簡潔具體。

你只提供評估,不會直接下單;部位大小永遠由風險模組重算,最後由人或核准閘門決定。"""


def _candles_csv(df: pd.DataFrame, n: int, digits: int) -> str:
    rows = ["time,open,high,low,close,volume"]
    for _, r in df.tail(n).iterrows():
        t = r["time"]
        ts = t.strftime("%Y-%m-%dT%H:%M") if hasattr(t, "strftime") else str(t)
        rows.append(f"{ts},{r['open']:.{digits}f},{r['high']:.{digits}f},{r['low']:.{digits}f},{r['close']:.{digits}f},{r['volume']:.0f}")
    return "\n".join(rows)


def _indicators(df: pd.DataFrame, digits: int) -> dict[str, float]:
    close = df["close"]
    return {
        "ema20": round(float(ema(close, 20).iloc[-1]), digits),
        "ema50": round(float(ema(close, 50).iloc[-1]), digits),
        "rsi14": round(float(rsi(close, 14).iloc[-1]), 1),
        "atr14": round(float(atr(df, 14).iloc[-1]), digits),
        "last_close": round(float(close.iloc[-1]), digits),
    }


class ClaudeAnalyst:
    name = "claude"

    def __init__(self, cfg: ResearchConfig, client: Any | None = None, fallback: Analyst | None = None, strict: bool = False):
        """``strict=True``(回測用):API 失敗直接拋出,不悄悄退回規則式。"""
        self.cfg = cfg
        self.fallback: Analyst = fallback or RuleBasedAnalyst()
        self.strict = strict
        self.calls = 0
        self.fallbacks_used = 0
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        if client is None:
            import anthropic

            client = anthropic.Anthropic(timeout=cfg.timeout_seconds, max_retries=cfg.max_retries)
        self.client = client

    # ------------------------------------------------------------------
    def build_payload(self, ctx: ResearchContext) -> str:
        inst = get_instrument(ctx.snapshot.symbol)
        digits = inst.digits
        data: dict[str, Any] = {
            "as_of": ctx.now.isoformat() if ctx.now else ctx.snapshot.time.isoformat(),
            "symbol": ctx.snapshot.symbol,
            "snapshot": ctx.snapshot.model_dump(mode="json"),
            "signals": [s.model_dump(mode="json") for s in ctx.signals],
            "mechanical_plan": ctx.plan.model_dump(mode="json"),
            "risk_checks": ctx.risk.model_dump(mode="json"),
            "open_positions": [
                {"symbol": p.symbol, "direction": p.direction.value, "lots": p.lots, "entry": p.entry_price, "stop": p.stop_loss, "target": p.take_profit, "risk_amount": p.risk_amount}
                for p in ctx.open_positions
                if p.status == "open"
            ],
            "recent_trades_this_symbol": ctx.recent_trades,
        }
        if ctx.candles:
            h1 = ctx.candles.get(Timeframe.H1)
            h4 = ctx.candles.get(Timeframe.H4)
            if h1 is not None and len(h1):
                data["indicators_h1"] = _indicators(h1, digits)
                data["candles_h1_csv"] = _candles_csv(h1, self.cfg.candles_h1, digits)
            if h4 is not None and len(h4):
                data["indicators_h4"] = _indicators(h4, digits)
                data["candles_h4_csv"] = _candles_csv(h4, self.cfg.candles_h4, digits)
        return "請評估以下候選交易設定:\n\n" + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1)

    def _use_fallback(self, ctx: ResearchContext, why: str) -> ResearchAssessment:
        if self.strict:
            raise RuntimeError(f"Claude 分析失敗(strict 模式不退回規則式):{why}")
        log.warning("Claude 分析改用規則式備援:%s", why)
        self.fallbacks_used += 1
        out = self.fallback.assess(ctx)
        out.concerns = [f"Claude 分析未完成({why}),以下為規則式評估"] + list(out.concerns)
        out.source = f"rules(fallback:{self.name})"
        return out

    def _record_usage(self, resp: Any) -> tuple[int, int]:
        usage = getattr(resp, "usage", None)
        tin = tout = 0
        if usage is not None:
            for key in self.usage:
                self.usage[key] += int(getattr(usage, key, 0) or 0)
            tin = int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "cache_read_input_tokens", 0) or 0) + int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            tout = int(getattr(usage, "output_tokens", 0) or 0)
        return tin, tout

    @property
    def estimated_cost_usd(self) -> float | None:
        return estimate_cost_usd(self.cfg.model, self.usage)

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

        self.calls += 1
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

        tin, tout = self._record_usage(resp)
        if getattr(resp, "stop_reason", None) == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return self._use_fallback(ctx, f"模型拒答 category={category}")

        parsed = getattr(resp, "parsed_output", None)
        if parsed is None:
            return self._use_fallback(ctx, f"無結構化輸出 stop_reason={getattr(resp, 'stop_reason', None)}")

        served_by = getattr(resp, "model", self.cfg.model)
        out = ResearchAssessment(**parsed.model_dump(), source=f"claude:{served_by}", tokens_in=tin, tokens_out=tout)
        # 界限:信念部位 0.25–1.0;關閉的能力一律清掉
        out.risk_fraction = min(1.0, max(0.25, float(out.risk_fraction))) if self.cfg.allow_conviction_sizing else 1.0
        if not self.cfg.allow_plan_adjustment:
            out.suggested_entry = out.suggested_stop_loss = out.suggested_take_profit = None
        return out
