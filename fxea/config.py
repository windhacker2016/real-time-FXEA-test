"""設定檔:YAML 覆蓋在預設值之上。"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class ScannerConfig(BaseModel):
    lookback: int = 200  # 掃描時取多少根 H1
    volume_ratio_min: float = 1.2  # 成交量放大門檻
    change_pct_alert: float = 0.25  # 單根 H1 變動 % 視為顯著
    level_proximity_atr: float = 0.5  # 距關鍵價位幾個 ATR 內視為「接近」
    news_window_minutes: int = 120  # 掃描時往後看幾分鐘的新聞


class SignalConfig(BaseModel):
    min_score: float = 60.0
    max_per_symbol: int = 2
    breakout_lookback: int = 20
    rsi_period: int = 14


class PlannerConfig(BaseModel):
    min_risk_reward: float = 2.0
    atr_stop_multiplier: float = 1.5  # 沒有結構時的預設停損距離
    max_stop_atr: float = 3.0  # 結構停損超過此距離改用 ATR 停損
    stop_buffer_atr: float = 0.1  # 停損放在結構外多少緩衝
    entry_zone_atr: float = 0.25  # 進場區半寬
    invalidation_atr: float = 0.5  # 失效價位距停損多遠
    target_atr_fallback: float = 3.0  # 找不到壓力位時的目標距離


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 1.0
    max_total_exposure_pct: float = 5.0  # 所有未平倉部位風險 % 加總上限
    max_open_positions: int = 4
    max_drawdown_pct: float = 10.0
    max_daily_loss_pct: float = 3.0
    volatility_percentile_band: tuple[float, float] = (5.0, 95.0)
    allow_same_symbol: bool = False
    news_blackout_minutes: int = 30


class ResearchConfig(BaseModel):
    provider: Literal["rules", "claude"] = "rules"
    model: str = "claude-fable-5-1"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    fallbacks: bool = True  # 啟用伺服器端拒答備援
    timeout_seconds: float = 180.0
    max_tokens: int = 4096


class ApprovalConfig(BaseModel):
    mode: Literal["human", "auto"] = "human"  # 圖 7:預設由你做最後決定
    pending_ttl_minutes: int = 240


class MonitorConfig(BaseModel):
    interval_seconds: float = 60.0
    proactive_review_every: int = 10  # 每幾輪做一次主動檢視
    status_every: int = 5  # 每幾輪發一次狀態摘要


class AccountConfig(BaseModel):
    currency: str = "USD"
    starting_balance: float = 10_000.0


class AlertConfig(BaseModel):
    console: bool = True
    file: Optional[str] = "alerts.jsonl"  # 相對 state_dir
    webhook_url: Optional[str] = None


class Settings(BaseModel):
    watchlist: list[str] = Field(default_factory=lambda: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])
    timeframes: list[str] = Field(default_factory=lambda: ["H1", "H4"])
    data_source: Literal["synthetic", "csv", "mt5"] = "synthetic"
    csv_dir: Optional[str] = None
    broker: Literal["paper", "mt5"] = "paper"
    state_dir: str = "state"
    news_file: Optional[str] = None
    synthetic_seed: int = 42
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
