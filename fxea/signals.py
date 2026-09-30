"""圖 3 訊號引擎:掃描 → 偵測 → 評分。

五種型態:突破、拉回、動能、趨勢延續、反轉。每個偵測器回傳 0–100 的分數,
最後依機率(分數)排序,標記高勝率交易機會。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import SignalConfig
from .indicators import (
    atr,
    ema,
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_hammer,
    is_shooting_star,
    rsi,
    slope,
    swing_levels,
    volume_ratio,
)
from .models import Direction, MarketSnapshot, Signal, SignalType, Timeframe


@dataclass
class _Ctx:
    close: float
    open: float
    high: float
    low: float
    prev_high: float
    prev_low: float
    ema20: float
    ema50: float
    ema20_slope: float
    atr: float
    rsi: float
    vr: float
    swing_high: float
    swing_low: float
    support: float
    resistance: float
    closes: list[float]


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


class SignalEngine:
    def __init__(self, cfg: SignalConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------
    def detect(self, snapshot: MarketSnapshot, candles: dict[Timeframe, pd.DataFrame]) -> list[Signal]:
        found: list[Signal] = []
        for tf in (Timeframe.H1, Timeframe.H4):
            df = candles.get(tf)
            if df is None or len(df) < 30:
                continue
            ctx = self._context(df)
            for detector in (
                self._breakout,
                self._pullback,
                self._momentum,
                self._trend_continuation,
                self._reversal,
            ):
                for direction in (Direction.LONG, Direction.SHORT):
                    sig = detector(snapshot, df, tf, ctx, direction)
                    if sig is not None:
                        found.append(sig)

        best: dict[tuple, Signal] = {}
        for s in found:
            key = (s.signal_type, s.direction, s.timeframe)
            if key not in best or s.score > best[key].score:
                best[key] = s
        ranked = sorted(best.values(), key=lambda s: s.score, reverse=True)
        ranked = [s for s in ranked if s.score >= self.cfg.min_score]
        # 實驗開關:只留特定型態 / 只留順勢
        if self.cfg.enabled_types is not None:
            allowed = set(self.cfg.enabled_types)
            ranked = [s for s in ranked if s.signal_type in allowed]
        if self.cfg.require_trend_alignment == "h4":
            ranked = [s for s in ranked if snapshot.trend_h4.matches(s.direction)]
        elif self.cfg.require_trend_alignment == "both":
            ranked = [s for s in ranked if snapshot.trend_h4.matches(s.direction) and snapshot.trend_h1.matches(s.direction)]
        return ranked[: self.cfg.max_per_symbol]

    # ------------------------------------------------------------------
    def _context(self, df: pd.DataFrame) -> _Ctx:
        close = df["close"]
        e20 = ema(close, 20)
        e50 = ema(close, 50)
        a = float(atr(df).iloc[-1])
        swing_high, swing_low = swing_levels(df, self.cfg.breakout_lookback)
        resistance, support = swing_levels(df, 50)
        return _Ctx(
            close=float(close.iloc[-1]),
            open=float(df["open"].iloc[-1]),
            high=float(df["high"].iloc[-1]),
            low=float(df["low"].iloc[-1]),
            prev_high=float(df["high"].iloc[-2]),
            prev_low=float(df["low"].iloc[-2]),
            ema20=float(e20.iloc[-1]),
            ema50=float(e50.iloc[-1]),
            ema20_slope=slope(e20, 5),
            atr=a if a > 0 else 1e-9,
            rsi=float(rsi(close, self.cfg.rsi_period).iloc[-1]),
            vr=volume_ratio(df),
            swing_high=swing_high,
            swing_low=swing_low,
            support=support,
            resistance=resistance,
            closes=[float(x) for x in close.tail(5)],
        )

    @staticmethod
    def _alignment(snapshot: MarketSnapshot, direction: Direction) -> tuple[float, list[str]]:
        bonus, reasons = 0.0, []
        if snapshot.trend_h4.matches(direction):
            bonus += 6
            reasons.append("H4 趨勢同向")
        elif snapshot.trend_h4.matches(direction.opposite):
            bonus -= 8
            reasons.append("H4 趨勢反向(扣分)")
        if snapshot.trend_h1.matches(direction):
            bonus += 4
            reasons.append("H1 趨勢同向")
        return bonus, reasons

    @staticmethod
    def _volume_bonus(vr: float) -> tuple[float, str | None]:
        if vr >= 1.5:
            return 8.0, f"成交量放大 {vr:.1f} 倍"
        if vr >= 1.2:
            return 4.0, f"成交量偏高 {vr:.1f} 倍"
        if vr < 0.7:
            return -4.0, f"成交量偏低 {vr:.1f} 倍(扣分)"
        return 0.0, None

    def _make(self, snapshot, tf, stype, direction, score, ref, level, reasons) -> Signal:
        return Signal(
            symbol=snapshot.symbol,
            timeframe=tf,
            signal_type=stype,
            direction=direction,
            score=round(_clamp(score), 1),
            reference_price=ref,
            key_level=level,
            reasons=reasons,
            time=snapshot.time,
        )

    # ---- 突破:價格突破關鍵價位 -----------------------------------------
    def _breakout(self, snapshot, df, tf, c: _Ctx, d: Direction):
        if d is Direction.LONG:
            level, broke = c.swing_high, c.close > c.swing_high and c.close > c.ema20
            dist = c.close - c.swing_high
        else:
            level, broke = c.swing_low, c.close < c.swing_low and c.close < c.ema20
            dist = c.swing_low - c.close
        if not broke or dist > 2.0 * c.atr:
            return None
        reasons = [f"收盤{'突破' if d is Direction.LONG else '跌破'} {self.cfg.breakout_lookback} 根關鍵價位 {level:.5g}"]
        score = 62.0 + min(12.0, dist / c.atr * 12.0)
        vb, vr_reason = self._volume_bonus(c.vr)
        score += vb
        if vr_reason:
            reasons.append(vr_reason)
        ab, ar = self._alignment(snapshot, d)
        score += ab
        reasons += ar
        return self._make(snapshot, tf, SignalType.BREAKOUT, d, score, c.close, level, reasons)

    # ---- 拉回:趨勢中的短暫回檔 -----------------------------------------
    def _pullback(self, snapshot, df, tf, c: _Ctx, d: Direction):
        zone = 0.3 * c.atr
        if d is Direction.LONG:
            trending = c.ema20 > c.ema50 and c.close > c.ema50
            touched = min(c.low, c.prev_low) <= c.ema20 + zone
            resumed = c.close > c.ema20 and c.close > c.open
        else:
            trending = c.ema20 < c.ema50 and c.close < c.ema50
            touched = max(c.high, c.prev_high) >= c.ema20 - zone
            resumed = c.close < c.ema20 and c.close < c.open
        if not (trending and touched and resumed):
            return None
        reasons = [f"趨勢中回測 EMA20({c.ema20:.5g})後恢復{'上漲' if d is Direction.LONG else '下跌'}"]
        score = 64.0
        if 40 <= c.rsi <= 60:
            score += 6
            reasons.append(f"RSI {c.rsi:.0f} 回到中性區")
        ab, ar = self._alignment(snapshot, d)
        score += ab
        reasons += ar
        vb, vr_reason = self._volume_bonus(c.vr)
        score += vb / 2
        if vr_reason:
            reasons.append(vr_reason)
        return self._make(snapshot, tf, SignalType.PULLBACK, d, score, c.close, c.ema20, reasons)

    # ---- 動能:價格強勁加速 ---------------------------------------------
    def _momentum(self, snapshot, df, tf, c: _Ctx, d: Direction):
        cl = c.closes
        if len(cl) < 4:
            return None
        if d is Direction.LONG:
            run = cl[-1] > cl[-2] > cl[-3] > cl[-4]
            move = cl[-1] - cl[-4]
            rsi_ok = c.rsi >= 60
            overextended = c.rsi > 80
        else:
            run = cl[-1] < cl[-2] < cl[-3] < cl[-4]
            move = cl[-4] - cl[-1]
            rsi_ok = c.rsi <= 40
            overextended = c.rsi < 20
        if not (run and move >= 1.5 * c.atr and rsi_ok):
            return None
        reasons = [f"連續 3 根同向,3 根累積 {move / c.atr:.1f} ATR", f"RSI {c.rsi:.0f}"]
        score = 58.0 + min(14.0, (move / c.atr - 1.5) * 10.0)
        if overextended:
            score -= 8
            reasons.append("RSI 過度延伸(扣分)")
        vb, vr_reason = self._volume_bonus(c.vr)
        score += vb
        if vr_reason:
            reasons.append(vr_reason)
        ab, ar = self._alignment(snapshot, d)
        score += ab
        reasons += ar
        return self._make(snapshot, tf, SignalType.MOMENTUM, d, score, c.close, None, reasons)

    # ---- 趨勢延續:趨勢維持並持續 ---------------------------------------
    def _trend_continuation(self, snapshot, df, tf, c: _Ctx, d: Direction):
        if d is Direction.LONG:
            ok = c.ema20 > c.ema50 and c.close > c.ema20 and c.ema20_slope > 0 and 50 <= c.rsi <= 70
            strength = c.ema20_slope / c.atr
        else:
            ok = c.ema20 < c.ema50 and c.close < c.ema20 and c.ema20_slope < 0 and 30 <= c.rsi <= 50
            strength = -c.ema20_slope / c.atr
        if not ok:
            return None
        reasons = ["EMA20/EMA50 多空排列且價格順勢", f"EMA20 斜率 {strength:.2f} ATR/根"]
        score = 60.0 + min(10.0, strength * 40.0)
        if (55 <= c.rsi <= 65) if d is Direction.LONG else (35 <= c.rsi <= 45):
            score += 5
            reasons.append(f"RSI {c.rsi:.0f} 健康")
        ab, ar = self._alignment(snapshot, d)
        score += ab
        reasons += ar
        return self._make(snapshot, tf, SignalType.TREND_CONTINUATION, d, score, c.close, c.ema20, reasons)

    # ---- 反轉:潛在趨勢反轉型態 -----------------------------------------
    def _reversal(self, snapshot, df, tf, c: _Ctx, d: Direction):
        if d is Direction.LONG:
            extreme = c.rsi <= 35
            engulf, pin = is_bullish_engulfing(df), is_hammer(df)
            near = c.low <= c.support + 0.5 * c.atr
            level = c.support
        else:
            extreme = c.rsi >= 65
            engulf, pin = is_bearish_engulfing(df), is_shooting_star(df)
            near = c.high >= c.resistance - 0.5 * c.atr
            level = c.resistance
        if not (extreme and (engulf or pin) and near):
            return None
        reasons = [
            f"RSI {c.rsi:.0f} 極端區",
            "吞噬型態" if engulf else ("錘子線" if d is Direction.LONG else "流星線"),
            f"位於{'支撐' if d is Direction.LONG else '壓力'}位 {level:.5g} 附近",
        ]
        score = 56.0 + (8 if (c.rsi <= 25 or c.rsi >= 75) else 0) + (8 if engulf else 5) + 6
        vb, vr_reason = self._volume_bonus(c.vr)
        score += vb
        if vr_reason:
            reasons.append(vr_reason)
        if snapshot.trend_h4.matches(d):
            score += 5
            reasons.append("H4 大趨勢同向(逆勢反轉轉為順勢回檔)")
        return self._make(snapshot, tf, SignalType.REVERSAL, d, score, c.close, level, reasons)
