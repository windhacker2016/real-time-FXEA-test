"""MetaTrader 5 橋接:建議的即時通道(IG 提供 MT5;官方 ``MetaTrader5`` Python 套件直連終端)。

需要 Windows + ``pip install MetaTrader5``,且 MT5 終端已登入 IG 帳戶。
不需要 API key、沒有歷史價格配額限制,停損/停利由伺服器端掛單。
此模組未在本開發環境(Linux)實測,僅依官方 Python 套件文件撰寫;請先用 IG 模擬帳戶跑通。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .data.base import MarketDataFeed
from .execution.base import Broker
from .indicators import COLUMNS
from .models import Account, Direction, Position, Timeframe, TradePlan
from .state import StateStore


def _mt5() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover - 平台相依
        raise RuntimeError("需要安裝 MetaTrader5 套件(僅 Windows)") from exc
    if not mt5.initialize():
        raise RuntimeError(f"MT5 初始化失敗:{mt5.last_error()}")
    return mt5


class MT5Feed(MarketDataFeed):
    def __init__(self, symbol_map: dict[str, str] | None = None) -> None:
        self.mt5 = _mt5()
        self.symbol_map = {k.upper(): v for k, v in (symbol_map or {}).items()}
        self._tf = {
            Timeframe.M15: self.mt5.TIMEFRAME_M15,
            Timeframe.H1: self.mt5.TIMEFRAME_H1,
            Timeframe.H4: self.mt5.TIMEFRAME_H4,
            Timeframe.D1: self.mt5.TIMEFRAME_D1,
        }

    def broker_symbol(self, symbol: str) -> str:
        return self.symbol_map.get(symbol.upper(), symbol)

    def candles(self, symbol: str, timeframe: Timeframe, n: int) -> pd.DataFrame:
        bsym = self.broker_symbol(symbol)
        self.mt5.symbol_select(bsym, True)
        rates = self.mt5.copy_rates_from_pos(bsym, self._tf[timeframe], 0, n)
        if rates is None:
            raise RuntimeError(f"MT5 取得 {bsym} K 棒失敗:{self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["volume"] = df["tick_volume"].astype(float)
        return df[COLUMNS].reset_index(drop=True)

    def price(self, symbol: str) -> float:
        tick = self.mt5.symbol_info_tick(self.broker_symbol(symbol))
        if tick is None:
            raise RuntimeError(f"MT5 取得 {symbol} 報價失敗")
        return (tick.bid + tick.ask) / 2.0

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class MT5Broker(Broker):
    name = "mt5"

    def __init__(
        self,
        state: StateStore,
        now: datetime,
        magic: int = 20260930,
        deviation: int = 20,
        symbol_map: dict[str, str] | None = None,
    ):
        self.mt5 = _mt5()
        self.state = state
        self.magic = magic
        self.deviation = deviation
        self.symbol_map = {k.upper(): v for k, v in (symbol_map or {}).items()}
        self._reverse = {v: k for k, v in self.symbol_map.items()}
        meta = state.read_json("mt5_meta.json") or {}
        self._tracked: dict[str, dict[str, Any]] = meta.get("tracked", {})
        self._peak: float | None = meta.get("peak_equity")
        self._day: str | None = meta.get("day")
        self._day_start_equity: float | None = meta.get("day_start_equity")
        self._rollover(now)

    def _save_meta(self) -> None:
        self.state.write_json(
            "mt5_meta.json",
            {"tracked": self._tracked, "peak_equity": self._peak, "day": self._day, "day_start_equity": self._day_start_equity},
        )

    def _rollover(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._day != today:
            self._day = today
            self._day_start_equity = None
            self._save_meta()

    def broker_symbol(self, symbol: str) -> str:
        return self.symbol_map.get(symbol.upper(), symbol)

    def account(self) -> Account:
        info = self.mt5.account_info()
        if info is None:
            raise RuntimeError("MT5 取得帳戶資訊失敗")
        equity = float(info.equity)
        if self._day_start_equity is None:
            self._day_start_equity = equity
        self._peak = max(self._peak or equity, equity)
        self._save_meta()
        return Account(
            currency=info.currency,
            balance=float(info.balance),
            equity=equity,
            peak_equity=float(self._peak),
            day=datetime.fromisoformat(self._day).date(),
            daily_pnl=round(equity - self._day_start_equity, 2),
        )

    def open_positions(self) -> list[Position]:
        out = []
        for p in self.mt5.positions_get() or []:
            meta = self._tracked.get(str(p.ticket), {})
            out.append(
                Position(
                    position_id=str(p.ticket),
                    symbol=self._reverse.get(p.symbol, p.symbol),
                    direction=Direction.LONG if p.type == self.mt5.POSITION_TYPE_BUY else Direction.SHORT,
                    lots=float(p.volume),
                    entry_price=float(p.price_open),
                    stop_loss=float(p.sl),
                    take_profit=float(p.tp),
                    opened_at=datetime.fromtimestamp(p.time, tz=timezone.utc),
                    memo_id=meta.get("memo_id"),
                    risk_amount=float(meta.get("risk_amount", 0.0)),
                )
            )
        return out

    def open_position(self, plan: TradePlan, lots: float, price: float, now: datetime, memo_id=None, risk_amount=0.0) -> Position:
        mt5 = self.mt5
        bsym = self.broker_symbol(plan.symbol)
        tick = mt5.symbol_info_tick(bsym)
        if tick is None:
            raise RuntimeError(f"MT5 取得 {bsym} 報價失敗")
        is_long = plan.direction is Direction.LONG
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": bsym,
            "volume": lots,
            "type": mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if is_long else tick.bid,
            "sl": plan.stop_loss,
            "tp": plan.take_profit,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": f"fxea {memo_id or ''}".strip(),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 下單失敗:{getattr(result, 'retcode', None)} {getattr(result, 'comment', '')}")
        ticket = str(result.order)
        self._tracked[ticket] = {"memo_id": memo_id, "risk_amount": risk_amount, "symbol": plan.symbol}
        self._save_meta()
        return Position(
            position_id=ticket,
            symbol=plan.symbol,
            direction=plan.direction,
            lots=lots,
            entry_price=float(result.price or price),
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            opened_at=now,
            memo_id=memo_id,
            risk_amount=risk_amount,
        )

    def close_position(self, position_id: str, price: float, now: datetime, reason: str) -> Position:
        mt5 = self.mt5
        pos = next((p for p in self.open_positions() if p.position_id == position_id), None)
        if pos is None:
            raise KeyError(f"MT5 沒有未平倉部位 {position_id}")
        bsym = self.broker_symbol(pos.symbol)
        tick = mt5.symbol_info_tick(bsym)
        if tick is None:
            raise RuntimeError(f"MT5 取得 {bsym} 報價失敗")
        is_long = pos.direction is Direction.LONG
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": bsym,
            "volume": pos.lots,
            "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
            "position": int(position_id),
            "price": tick.bid if is_long else tick.ask,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": f"fxea close {reason}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(f"MT5 平倉失敗:{getattr(result, 'retcode', None)}")
        pos.status = "closed"
        pos.closed_at = now
        pos.close_price = float(result.price or price)
        pos.close_reason = reason
        self._tracked.pop(position_id, None)
        self._save_meta()
        return pos

    def mark_to_market(self, prices: dict[str, float], now: datetime) -> list[Position]:
        self._rollover(now)
        live = {p.position_id for p in self.open_positions()}
        closed = []
        for ticket, meta in list(self._tracked.items()):
            if ticket not in live:
                closed.append(
                    Position(
                        position_id=ticket,
                        symbol=meta.get("symbol", "?"),
                        direction=Direction.LONG,
                        lots=0.0,
                        entry_price=0.0,
                        stop_loss=0.0,
                        take_profit=0.0,
                        opened_at=now,
                        memo_id=meta.get("memo_id"),
                        risk_amount=float(meta.get("risk_amount", 0.0)),
                        status="closed",
                        closed_at=now,
                        close_reason="MT5 端已平倉",
                    )
                )
                self._tracked.pop(ticket, None)
        if closed:
            self._save_meta()
        return closed
