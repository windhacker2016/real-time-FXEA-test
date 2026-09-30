"""設定檔:YAML 覆蓋在預設值之上。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .models import SignalType


class ScannerConfig(BaseModel):
    lookback: int = 200  # 掃描時取多少根 H1
    volume_ratio_min: float = 1.2  # 成交量放大門檻
    change_pct_alert: float = 0.25  # 單根 H1 變動 % 視為顯著
    level_proximity_atr: float = 0.5  # 距關鍵價位幾個 ATR 內視為「接近」
    news_window_minutes: int = 120  # 掃描時往後看幾分鐘的新聞
    # 交易時段(UTC 小時,含起不含迄;可跨日如 (22, 6)):時段外不標記機會,但部位照常監控
    trading_hours_utc: Optional[tuple[int, int]] = None


class SignalConfig(BaseModel):
    min_score: float = 60.0
    max_per_symbol: int = 2
    breakout_lookback: int = 20
    rsi_period: int = 14
    enabled_types: Optional[list[SignalType]] = None  # None = 五種全開;實驗時可只留一種
    require_trend_alignment: Literal["none", "h4", "both"] = "none"  # 只留與 H4(或 H1+H4)趨勢同向的訊號


class PlannerConfig(BaseModel):
    min_risk_reward: float = 2.0
    atr_stop_multiplier: float = 1.5  # 沒有結構時的預設停損距離
    max_stop_atr: float = 3.0  # 結構停損超過此距離改用 ATR 停損
    min_stop_atr: float = 0.5  # AI 調整停損的下限
    stop_buffer_atr: float = 0.1  # 停損放在結構外多少緩衝
    entry_zone_atr: float = 0.25  # 進場區半寬
    invalidation_atr: float = 0.5  # 失效價位距停損多遠
    target_atr_fallback: float = 3.0  # 找不到壓力位時的目標距離
    max_entry_shift_atr: float = 1.0  # AI 調整進場價的上限


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 1.0
    max_total_exposure_pct: float = 5.0  # 所有未平倉部位風險 % 加總上限
    max_open_positions: int = 4
    max_drawdown_pct: float = 10.0
    max_daily_loss_pct: float = 3.0
    volatility_percentile_band: tuple[float, float] = (5.0, 95.0)
    allow_same_symbol: bool = False
    news_blackout_minutes: int = 30
    # 回撤斷路器:觸及 max_drawdown_pct → 暫停開新倉。
    # drawdown_cooldown_hours > 0:冷卻期滿自動以縮減風險恢復;0:只能人工 `fxea resume`
    drawdown_cooldown_hours: int = 0
    recovery_risk_scale: float = 0.5  # 恢復期每筆風險縮放,直到權益回到暫停前高點


class ResearchConfig(BaseModel):
    provider: Literal["rules", "claude"] = "rules"
    model: str = "claude-fable-5-1"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    fallbacks: bool = True  # 啟用伺服器端拒答備援
    timeout_seconds: float = 180.0
    max_retries: int = 2  # SDK 對連線錯誤 / 429 / 5xx 的重試次數
    max_tokens: int = 4096
    # AI 看得到什麼
    candles_h1: int = 60
    candles_h4: int = 40
    recent_trades: int = 10  # 同商品近期交易結果(它自己的戰績)
    # AI 能決定什麼
    allow_plan_adjustment: bool = True  # 在界限內調整進場/停損/目標(風險模組重新驗算)
    allow_conviction_sizing: bool = True  # 依信念縮小部位:risk_fraction 0.25–1.0
    only_when_ready: bool = True  # 只對通過風險檢查的設定呼叫 AI;被阻擋的用規則式(省錢)


class ApprovalConfig(BaseModel):
    mode: Literal["human", "auto"] = "human"  # 圖 7:預設由你做最後決定
    pending_ttl_minutes: int = 240
    # auto 模式是否採納研究建議:execute → 核准、watch → 觀察清單(進入進場區才核准)、skip → 拒絕
    honor_research: bool = False


class MonitorConfig(BaseModel):
    interval_seconds: float = 60.0
    proactive_review_every: int = 10  # 每幾輪做一次主動檢視
    status_every: int = 5  # 每幾輪發一次狀態摘要
    block_cooldown_minutes: int = 240  # 同商品、同樣風險阻擋原因在此期間內不重複產生備忘錄
    min_minutes_between_memos: int = 60  # 同商品兩份備忘錄的最短間隔(即時模式控制 AI 呼叫頻率)


class AccountConfig(BaseModel):
    currency: str = "USD"
    starting_balance: float = 10_000.0
    spread_pips: Optional[float] = None  # 紙上交易點差;None = 用商品預設,0 = 不計點差


class AlertConfig(BaseModel):
    console: bool = True
    file: Optional[str] = "alerts.jsonl"  # 相對 state_dir
    webhook_url: Optional[str] = None


class IGConfig(BaseModel):
    """IG REST API(備用通道;主要通道建議用 IG 提供的 MT5)。"""

    demo: bool = True
    account_id: Optional[str] = None
    epics: dict[str, str] = Field(
        default_factory=lambda: {
            "EURUSD": "CS.D.EURUSD.MINI.IP",
            "GBPUSD": "CS.D.GBPUSD.MINI.IP",
            "USDJPY": "CS.D.USDJPY.MINI.IP",
            "AUDUSD": "CS.D.AUDUSD.MINI.IP",
        }
    )


class MT5Config(BaseModel):
    magic: int = 20260930
    deviation: int = 20  # 允許滑價(點)
    symbol_map: dict[str, str] = Field(default_factory=dict)  # 例如 {"EURUSD": "EURUSD.i"}


class InstrumentOverride(BaseModel):
    pip_size: Optional[float] = None
    digits: Optional[int] = None
    contract_size: Optional[float] = None  # IG 迷你合約 = 10000
    lot_min: Optional[float] = None
    lot_max: Optional[float] = None
    lot_step: Optional[float] = None
    spread_pips: Optional[float] = None


class Settings(BaseModel):
    watchlist: list[str] = Field(default_factory=lambda: ["EURUSD"])
    timeframes: list[str] = Field(default_factory=lambda: ["H1", "H4"])
    data_source: Literal["synthetic", "csv", "mt5", "ig"] = "synthetic"
    csv_dir: Optional[str] = None
    broker: Literal["paper", "mt5", "ig"] = "paper"
    state_dir: str = "state"
    news_file: Optional[str] = None
    synthetic_seed: int = 42
    instruments: dict[str, InstrumentOverride] = Field(default_factory=dict)
    ig: IGConfig = Field(default_factory=IGConfig)
    mt5: MT5Config = Field(default_factory=MT5Config)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    signals: SignalConfig = Field(default_factory=SignalConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def set_dotted(target: dict, dotted: str, value) -> dict:
    """把 ``a.b.c=value`` 放進巢狀 dict(給 CLI ``--set`` 用)。"""
    keys = dotted.split(".")
    node = target
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            raise ValueError(f"--set {dotted}:{k} 不是設定區塊")
    node[keys[-1]] = value
    return target


def parse_set_args(items: list[str] | None) -> dict:
    """``["signals.min_score=70", "scanner.trading_hours_utc=[7,17]"]`` → 巢狀覆蓋;值以 YAML 解析。"""
    out: dict = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--set 需要 key=value,收到 {item!r}")
        key, raw = item.split("=", 1)
        set_dotted(out, key.strip(), yaml.safe_load(raw))
    return out


def load_settings(path: str | Path | None = None, overrides: dict | None = None) -> Settings:
    data: dict = {}
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"找不到設定檔:{p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if overrides:
        data = _deep_merge(data, overrides)
    return Settings.model_validate(data)
