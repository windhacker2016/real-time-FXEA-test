"""圖 2 市場掃描器:價格走勢、成交量、趨勢、新聞、自選清單 → 偵測到交易機會。

模式:掃描 / 頻率:持續 / 時間週期:全天候。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from .config import ScannerConfig
from .data.base import MarketDataFeed, NewsFeed
from .indicators import atr, detect_trend, percentile_rank, rsi, swing_levels, volume_ratio
from .instruments import get_instrument
from .models import KeyLevels, MarketSnapshot, Timeframe, Trend

log = logging.getLogger("fxea.scanner")


@dataclass
class ScanResult:
    snapshot: MarketSnapshot
    candles: dict[Timeframe, pd.DataFrame] = field(default_factory=dict)


class MarketScanner:
    def __init__(self, feed: MarketDataFeed, cfg: ScannerConfig, news: NewsFeed | None = None):
        self.feed = feed
        self.cfg = cfg
        self.news = news
        self.errors: dict[str, str] = {}

    def scan(self, symbol: str) -> ScanResult:
        cfg = self.cfg
        symbol = symbol.upper()
        h1 = self.feed.candles(symbol, Timeframe.H1, cfg.lookback)
        h4 = self.feed.candles(symbol, Timeframe.H4, max(60, cfg.lookback // 4))
        if len(h1) < 30:
            raise ValueError(f"{symbol} H1 資料不足({len(h1)} 根)")

        now = self.feed.now()
        price = float(h1["close"].iloc[-1])
        prev_close = float(h1["close"].iloc[-2])
        change_pct = (price / prev_close - 1.0) * 100.0 if prev_close else 0.0
        vr = volume_ratio(h1)
        trend_h1 = detect_trend(h1)
        trend_h4 = detect_trend(h4)

        atr_h1_series = atr(h1)
        atr_h1 = float(atr_h1_series.iloc[-1])
        atr_h4 = float(atr(h4).iloc[-1])
        atr_pct = percentile_rank(atr_h1_series.tail(cfg.lookback), atr_h1)
        rsi_h1 = float(rsi(h1["close"]).iloc[-1])

        swing_high, swing_low = swing_levels(h1, 20)
        resistance, support = swing_levels(h1, 50)
        inst = get_instrument(symbol)
        levels = KeyLevels(
            support=inst.round_price(support),
            resistance=inst.round_price(resistance),
            swing_low=inst.round_price(swing_low),
            swing_high=inst.round_price(swing_high),
        )

        news = []
        if self.news is not None:
            news = self.news.upcoming(now, cfg.news_window_minutes, {inst.base, inst.quote})

        # ---- 五個掃描面向,累積「機會分數」 --------------------------------
        notes: list[str] = []
        points = 0
        if abs(change_pct) >= cfg.change_pct_alert:
            notes.append(f"價格走勢:最新 H1 變動 {change_pct:+.2f}%")
            points += 1
        if vr >= cfg.volume_ratio_min:
            notes.append(f"成交量:為近期平均 {vr:.2f} 倍")
            points += 1
        if trend_h1 is not Trend.SIDEWAYS:
            notes.append(f"趨勢:H1 {trend_h1.label_zh}")
            points += 1
        if trend_h4 is not Trend.SIDEWAYS and trend_h4 is trend_h1:
            notes.append(f"趨勢:H4 與 H1 一致({trend_h4.label_zh})")
            points += 1
        prox = cfg.level_proximity_atr * atr_h1
        if abs(price - levels.resistance) <= prox:
            notes.append(f"價位:接近壓力位 {levels.resistance}")
            points += 1
        elif abs(price - levels.support) <= prox:
            notes.append(f"價位:接近支撐位 {levels.support}")
            points += 1
        for e in news:
            notes.append(f"新聞:{e.time:%m/%d %H:%M} {e.currency} {e.title}(影響:{e.impact})")

        opportunity = points >= 2
        if opportunity:
            notes.append("偵測到交易機會")

        snapshot = MarketSnapshot(
            symbol=symbol,
            time=now,
            price=price,
            change_pct=round(change_pct, 4),
            volume_ratio=round(vr, 3),
            trend_h1=trend_h1,
            trend_h4=trend_h4,
            atr_h1=atr_h1,
            atr_h4=atr_h4,
            atr_percentile=round(atr_pct, 1),
            rsi_h1=round(rsi_h1, 1),
            levels=levels,
            upcoming_news=news,
            opportunity=opportunity,
            notes=notes,
        )
        return ScanResult(snapshot=snapshot, candles={Timeframe.H1: h1, Timeframe.H4: h4})

    def scan_all(self, symbols: list[str]) -> list[ScanResult]:
        out: list[ScanResult] = []
        self.errors = {}
        for sym in symbols:
            try:
                out.append(self.scan(sym))
            except Exception as exc:  # 單一商品失敗不能讓整個循環停下來
                self.errors[sym.upper()] = str(exc)
                log.warning("掃描 %s 失敗:%s", sym, exc)
        return out
