"""圖 4 交易規劃器:進場區、獲利目標、停損、失效條件、風險/報酬、方向、時間週期、信心。

- 進場區:辨識最佳進場範圍(參考價 ± entry_zone_atr × ATR)
- 停損:在結構下方設定風險(擺盪低點 − 緩衝;過遠則改用 ATR 倍數)
- 獲利目標:目標設於關鍵壓力位(找不到合適壓力位則用 R:R / ATR 推算)
- 失效條件:價位遭突破時,交易設定失效(停損再往外 invalidation_atr × ATR)
"""
from __future__ import annotations

import pandas as pd

from .config import PlannerConfig
from .indicators import atr, swing_levels
from .instruments import get_instrument
from .models import Confidence, Direction, MarketSnapshot, Signal, SignalType, Timeframe, TradePlan


class TradePlanner:
    def __init__(self, cfg: PlannerConfig):
        self.cfg = cfg

    def build(self, signal: Signal, snapshot: MarketSnapshot, candles: dict[Timeframe, pd.DataFrame]) -> TradePlan:
        cfg = self.cfg
        df = candles[signal.timeframe]
        inst = get_instrument(signal.symbol)
        a = float(atr(df).iloc[-1])
        if a <= 0:
            a = inst.pip_size * 10
        d = signal.direction
        sgn = d.sign
        price = snapshot.price
        rationale: list[str] = []

        # ---- 進場區 ------------------------------------------------------
        entry = price
        if signal.signal_type is SignalType.PULLBACK and signal.key_level is not None:
            if abs(signal.key_level - price) <= 1.0 * a:
                entry = (price + signal.key_level) / 2.0
                rationale.append("進場區:拉回型態,取現價與 EMA20 中點")
        if entry == price:
            rationale.append("進場區:以現價為中心")
        half = cfg.entry_zone_atr * a
        zone_low, zone_high = entry - half, entry + half

        # ---- 停損(結構下方 / 上方)-------------------------------------
        swing_high, swing_low = swing_levels(df, 20)
        structure = swing_low if d is Direction.LONG else swing_high
        structure_stop = structure - sgn * cfg.stop_buffer_atr * a
        structure_dist = (entry - structure_stop) * sgn
        atr_stop = entry - sgn * cfg.atr_stop_multiplier * a
        atr_dist = cfg.atr_stop_multiplier * a
        if 0 < structure_dist <= cfg.max_stop_atr * a:
            stop, stop_dist, stop_note = structure_stop, structure_dist, f"停損:結構{'低' if d is Direction.LONG else '高'}點外 {cfg.stop_buffer_atr} ATR"
        else:
            stop, stop_dist, stop_note = atr_stop, atr_dist, f"停損:結構價位不適用,改用 {cfg.atr_stop_multiplier} ATR"

        # ---- 獲利目標(關鍵壓力/支撐位;否則以 ATR 推算)----------------
        key_target = snapshot.levels.resistance if d is Direction.LONG else snapshot.levels.support
        level_dist = (key_target - entry) * sgn

        def _target(stop_dist_: float) -> tuple[float, str, bool]:
            if level_dist > 0 and level_dist / stop_dist_ >= cfg.min_risk_reward:
                return key_target, f"目標:設於關鍵{'壓力' if d is Direction.LONG else '支撐'}位 {key_target}", False
            reward = cfg.target_atr_fallback * a
            blocked = 0 < level_dist < reward
            return entry + sgn * reward, "目標:無合適關鍵價位,以 ATR 推算", blocked

        target, target_note, obstacle = _target(stop_dist)
        rr = ((target - entry) * sgn) / stop_dist if stop_dist > 0 else 0.0
        # 結構停損太寬、達不到最低 R:R → 退回 ATR 停損再試一次
        if rr < cfg.min_risk_reward and stop is not atr_stop and atr_dist < stop_dist:
            stop, stop_dist = atr_stop, atr_dist
            stop_note = f"停損:結構停損無法達到 R:R,改用 {cfg.atr_stop_multiplier} ATR"
            target, target_note, obstacle = _target(stop_dist)
            rr = ((target - entry) * sgn) / stop_dist
        rationale.append(stop_note)
        rationale.append(target_note)
        if obstacle:
            rationale.append(f"注意:途中有{'壓力' if d is Direction.LONG else '支撐'}位 {key_target}")

        # ---- 失效條件 ----------------------------------------------------
        invalidation = stop - sgn * cfg.invalidation_atr * a
        cond = (
            f"{signal.timeframe.value} 收盤{'跌破' if d is Direction.LONG else '突破'} "
            f"{inst.round_price(invalidation)}(結構{'低' if d is Direction.LONG else '高'}點之外)則此設定失效"
        )

        # ---- 信心 --------------------------------------------------------
        if signal.score >= 80:
            conf = Confidence.HIGH
        elif signal.score >= 68:
            conf = Confidence.MEDIUM
        else:
            conf = Confidence.LOW
        if obstacle and conf is not Confidence.LOW:
            conf = Confidence.MEDIUM if conf is Confidence.HIGH else Confidence.LOW

        valid, reason = True, None
        if rr < cfg.min_risk_reward:
            valid, reason = False, f"風險報酬比 1:{rr:.2f} 低於下限 1:{cfg.min_risk_reward}"

        return TradePlan(
            symbol=signal.symbol,
            direction=d,
            timeframe=signal.timeframe,
            signal_type=signal.signal_type,
            entry_price=inst.round_price(entry),
            entry_zone_low=inst.round_price(zone_low),
            entry_zone_high=inst.round_price(zone_high),
            stop_loss=inst.round_price(stop),
            take_profit=inst.round_price(target),
            invalidation_level=inst.round_price(invalidation),
            invalidation_condition=cond,
            risk_reward=round(rr, 2),
            confidence=conf,
            atr=a,
            rationale=rationale,
            valid=valid,
            invalid_reason=reason,
        )
