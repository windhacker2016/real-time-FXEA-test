"""FXEA 資料模型。

每個模型都對應概念圖上的一個節點:
- 圖 2 市場掃描器  → :class:`MarketSnapshot`
- 圖 3 訊號引擎    → :class:`Signal`
- 圖 4 交易規劃器  → :class:`TradePlan`
- 圖 5 風險模組    → :class:`RiskReport`
- 圖 1 研究        → :class:`ResearchAssessment`
- 圖 7 最終決策備忘錄 → :class:`DecisionMemo`
- 圖 6 監控循環    → :class:`CycleReport` / :class:`Alert`
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 列舉
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def label_zh(self) -> str:
        return "做多" if self is Direction.LONG else "做空"

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class Timeframe(str, Enum):
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def minutes(self) -> int:
        return {"M15": 15, "H1": 60, "H4": 240, "D1": 1440}[self.value]

    @property
    def label_zh(self) -> str:
        return {"M15": "15 分鐘", "H1": "1 小時", "H4": "4 小時", "D1": "日線"}[self.value]


class Trend(str, Enum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"

    @property
    def label_zh(self) -> str:
        return {"up": "上升", "down": "下降", "sideways": "盤整"}[self.value]

    def matches(self, direction: Direction) -> bool:
        return (self is Trend.UP and direction is Direction.LONG) or (
            self is Trend.DOWN and direction is Direction.SHORT
        )


class SignalType(str, Enum):
    """圖 3 的五種訊號型態。"""

    BREAKOUT = "breakout"  # 突破:價格突破關鍵價位
    PULLBACK = "pullback"  # 拉回:趨勢中的短暫回檔
    MOMENTUM = "momentum"  # 動能:價格強勁加速
    TREND_CONTINUATION = "trend_continuation"  # 趨勢延續:趨勢維持並持續
    REVERSAL = "reversal"  # 反轉:潛在趨勢反轉型態

    @property
    def label_zh(self) -> str:
        return {
            "breakout": "突破",
            "pullback": "拉回",
            "momentum": "動能",
            "trend_continuation": "趨勢延續",
            "reversal": "反轉",
        }[self.value]


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def label_zh(self) -> str:
        return {"low": "低", "medium": "中", "high": "高"}[self.value]


class RiskRating(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def label_zh(self) -> str:
        return {"low": "低", "medium": "中等", "high": "高"}[self.value]


class MarketRegime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"

    @property
    def label_zh(self) -> str:
        return {"trending": "趨勢", "ranging": "盤整", "volatile": "高波動"}[self.value]


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class MemoStatus(str, Enum):
    READY = "ready"  # 可進行決策(所有檢查皆通過,等待人工核准)
    BLOCKED = "blocked"  # 風險模組未通過 → 阻擋
    EXPIRED = "expired"  # 超過等待時限或失效條件觸發


class Decision(str, Enum):
    PENDING = "pending"  # 需要人工審核
    APPROVE = "approve"  # 核准 → 執行交易
    WATCHLIST = "watchlist"  # 觀察清單 → 監控條件
    REJECT = "reject"  # 拒絕 → 不要交易

    @property
    def label_zh(self) -> str:
        return {
            "pending": "待審核",
            "approve": "核准",
            "watchlist": "觀察清單",
            "reject": "拒絕",
        }[self.value]


# ---------------------------------------------------------------------------
# 市場資料
# ---------------------------------------------------------------------------


class Candle(BaseModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class NewsEvent(BaseModel):
    """圖 2「新聞:監控新聞與事件」的一筆事件。"""

    time: datetime
    currency: str
    impact: Literal["low", "medium", "high"]
    title: str


class KeyLevels(BaseModel):
    support: float
    resistance: float
    swing_low: float
    swing_high: float


class MarketSnapshot(BaseModel):
    """圖 2 市場掃描器對單一商品的輸出。"""

    symbol: str
    time: datetime
    price: float
    change_pct: float  # 價格走勢:相對前一根 H1 收盤的變動 %
    volume_ratio: float  # 成交量:最新一根相對近期平均
    trend_h1: Trend
    trend_h4: Trend
    atr_h1: float
    atr_h4: float
    atr_percentile: float  # ATR 在回看區間內的百分位(0-100)
    rsi_h1: float
    levels: KeyLevels
    upcoming_news: list[NewsEvent] = Field(default_factory=list)
    opportunity: bool = False  # 偵測到交易機會
    notes: list[str] = Field(default_factory=list)

    @property
    def news_risk(self) -> bool:
        return any(e.impact == "high" for e in self.upcoming_news)


# ---------------------------------------------------------------------------
# 訊號 / 計畫 / 風險 / 研究
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """圖 3:已標記的高勝率交易機會。"""

    symbol: str
    timeframe: Timeframe
    signal_type: SignalType
    direction: Direction
    score: float = Field(ge=0, le=100)  # 依機率排序用的評分
    reference_price: float
    key_level: Optional[float] = None
    reasons: list[str] = Field(default_factory=list)
    time: datetime = Field(default_factory=utcnow)


class TradePlan(BaseModel):
    """圖 4:進場、目標、停損、失效條件。"""

    symbol: str
    direction: Direction
    timeframe: Timeframe
    signal_type: SignalType
    entry_price: float
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    take_profit: float
    invalidation_level: float
    invalidation_condition: str
    risk_reward: float
    confidence: Confidence
    atr: float
    rationale: list[str] = Field(default_factory=list)
    valid: bool = True
    invalid_reason: Optional[str] = None

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit - self.entry_price)

    def price_in_entry_zone(self, price: float) -> bool:
        return self.entry_zone_low <= price <= self.entry_zone_high

    def is_invalidated(self, price: float) -> bool:
        """價位遭突破時,交易設定失效。"""
        if self.direction is Direction.LONG:
            return price <= self.invalidation_level
        return price >= self.invalidation_level


class RiskCheck(BaseModel):
    name: str
    label_zh: str
    passed: bool
    detail: str
    value: Optional[float] = None
    limit: Optional[float] = None


class RiskReport(BaseModel):
    """圖 5:五項檢查 → 通過/阻擋。"""

    checks: list[RiskCheck]
    passed: bool
    lots: float
    risk_amount: float
    risk_pct: float
    rating: RiskRating
    blocked_reasons: list[str] = Field(default_factory=list)

    def check(self, name: str) -> RiskCheck:
        for c in self.checks:
            if c.name == name:
                return c
        raise KeyError(name)


class AnalystOutput(BaseModel):
    """研究分析器的結構化輸出(也是給 Claude 的 JSON schema)。

    對應圖 7 備忘錄的第 1–3 區塊。
    """

    trend_aligned: bool  # 趨勢一致
    market_regime: MarketRegime  # 市況
    confidence_score: Literal[1, 2, 3, 4, 5]  # 信心分數(★ 數)
    strength: Literal["weak", "moderate", "strong"]  # 強度
    max_loss_within_plan: bool  # 最大虧損符合計畫
    recommendation: Literal["execute", "watch", "skip"]  # 執行 / 觀察 / 略過
    rationale: str
    concerns: list[str] = Field(default_factory=list)


class ResearchAssessment(AnalystOutput):
    source: str = "rules"  # "rules" 或 "claude:<model>"

    @property
    def stars(self) -> str:
        return "★" * self.confidence_score + "☆" * (5 - self.confidence_score)

    @property
    def strength_zh(self) -> str:
        return {"weak": "弱", "moderate": "中等", "strong": "強"}[self.strength]

    @property
    def recommendation_zh(self) -> str:
        return {"execute": "執行", "watch": "觀察", "skip": "略過"}[self.recommendation]


# ---------------------------------------------------------------------------
# 最終決策備忘錄(圖 7)
# ---------------------------------------------------------------------------


class DecisionMemo(BaseModel):
    memo_id: str
    created_at: datetime
    symbol: str
    snapshot: MarketSnapshot
    signals: list[Signal]
    plan: TradePlan
    risk: RiskReport
    research: ResearchAssessment
    status: MemoStatus
    decision: Decision = Decision.PENDING
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    executed: bool = False
    position_id: Optional[str] = None
    note: Optional[str] = None

    @property
    def primary_signal(self) -> Signal:
        return self.signals[0]

    @property
    def awaiting_human(self) -> bool:
        return self.status is MemoStatus.READY and self.decision is Decision.PENDING

    @property
    def timeframes_zh(self) -> str:
        tfs = sorted({s.timeframe for s in self.signals}, key=lambda t: t.minutes)
        return " / ".join(t.label_zh for t in tfs) if tfs else self.plan.timeframe.label_zh


# ---------------------------------------------------------------------------
# 執行 / 帳戶 / 監控
# ---------------------------------------------------------------------------


class Position(BaseModel):
    position_id: str
    symbol: str
    direction: Direction
    lots: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    memo_id: Optional[str] = None
    risk_amount: float = 0.0
    status: Literal["open", "closed"] = "open"
    closed_at: Optional[datetime] = None
    close_price: Optional[float] = None
    pnl: float = 0.0
    close_reason: Optional[str] = None


class Account(BaseModel):
    currency: str = "USD"
    balance: float
    equity: float
    peak_equity: float
    day: date
    daily_pnl: float = 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity * 100.0)


class Alert(BaseModel):
    time: datetime = Field(default_factory=utcnow)
    level: AlertLevel = AlertLevel.INFO
    title: str
    message: str
    symbol: Optional[str] = None


class CycleReport(BaseModel):
    """圖 6 每一輪監控循環的摘要。"""

    cycle: int
    time: datetime
    scanned: int = 0
    opportunities: int = 0
    signals: int = 0
    memos_created: list[str] = Field(default_factory=list)
    memos_blocked: list[str] = Field(default_factory=list)
    executed: list[str] = Field(default_factory=list)
    closed: list[str] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)
    equity: float = 0.0
    drawdown_pct: float = 0.0
    open_positions: int = 0
    pending_memos: int = 0
