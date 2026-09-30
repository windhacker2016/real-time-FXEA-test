"""紙上交易券商:在本機模擬成交、停損/停利與權益曲線。

回測保真度:
- 點差:買在賣價、賣在買價(``spread_pips``,預設取商品規格),來回成本 = 一個點差
- 盤中觸價:若提供最新 K 棒且該棒在進場之後開始,用高/低點判斷停損/停利;
  同一根同時觸及兩者時**保守假設先停損**
"""
from __future__ import annotations

from datetime import datetime

from ..instruments import get_instrument
from ..models import Account, Candle, Direction, Position, TradePlan
from ..state import StateStore
from .base import Broker


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, state: StateStore, starting_balance: float, currency: str, now: datetime, spread_pips: float | None = None):
        self.state = state
        self.spread_override = spread_pips
        acct = state.load_account()
        if acct is None:
            acct = Account(
                currency=currency,
                balance=starting_balance,
                equity=starting_balance,
                peak_equity=starting_balance,
                day=now.date(),
            )
            state.save_account(acct)
        self._account = acct
        self._positions: list[Position] = state.load_positions()

    # ------------------------------------------------------------------
    def account(self) -> Account:
        return self._account

    def open_positions(self) -> list[Position]:
        return [p for p in self._positions if p.status == "open"]

    def all_positions(self) -> list[Position]:
        return list(self._positions)

    def _half_spread(self, symbol: str) -> float:
        inst = get_instrument(symbol)
        pips = inst.spread_pips if self.spread_override is None else self.spread_override
        return pips * inst.pip_size / 2.0

    def _exit_price(self, pos: Position, mid: float) -> float:
        """多單在買價出場、空單在賣價出場。"""
        return mid - pos.direction.sign * self._half_spread(pos.symbol)

    def _pnl_at_fill(self, pos: Position, fill: float) -> float:
        inst = get_instrument(pos.symbol)
        pips = (fill - pos.entry_price) * pos.direction.sign / inst.pip_size
        return pips * inst.pip_value(fill, self._account.currency) * pos.lots

    def _pnl(self, pos: Position, mid: float) -> float:
        return self._pnl_at_fill(pos, self._exit_price(pos, mid))

    def _save(self) -> None:
        self.state.save_account(self._account)
        self.state.save_positions(self._positions)

    # ------------------------------------------------------------------
    def open_position(self, plan: TradePlan, lots: float, price: float, now: datetime, memo_id=None, risk_amount=0.0) -> Position:
        inst = get_instrument(plan.symbol)
        fill = inst.round_price(price + plan.direction.sign * self._half_spread(plan.symbol))
        seq = self.state.next_seq("position")
        pos = Position(
            position_id=f"P-{seq:04d}",
            symbol=plan.symbol,
            direction=plan.direction,
            lots=lots,
            entry_price=fill,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            opened_at=now,
            memo_id=memo_id,
            risk_amount=risk_amount,
        )
        self._positions.append(pos)
        self._save()
        self.state.journal({"type": "open", "position_id": pos.position_id, "symbol": pos.symbol, "direction": pos.direction.value, "lots": lots, "price": fill})
        return pos

    def close_position(self, position_id: str, price: float, now: datetime, reason: str) -> Position:
        """``price`` 是觸發價位(中價);實際成交再扣半個點差。"""
        pos = next((p for p in self._positions if p.position_id == position_id and p.status == "open"), None)
        if pos is None:
            raise KeyError(f"沒有未平倉部位 {position_id}")
        inst = get_instrument(pos.symbol)
        fill = inst.round_price(self._exit_price(pos, price))
        pnl = round(self._pnl_at_fill(pos, fill), 2)
        pos.status = "closed"
        pos.closed_at = now
        pos.close_price = fill
        pos.pnl = pnl
        pos.close_reason = reason
        self._account.balance = round(self._account.balance + pnl, 2)
        self._account.daily_pnl = round(self._account.daily_pnl + pnl, 2)
        self._save()
        self.state.journal({"type": "close", "position_id": pos.position_id, "symbol": pos.symbol, "price": fill, "pnl": pnl, "reason": reason})
        return pos

    def mark_to_market(self, prices: dict[str, float], now: datetime, bars: dict[str, Candle] | None = None) -> list[Position]:
        if now.date() != self._account.day:
            self._account.day = now.date()
            self._account.daily_pnl = 0.0

        closed: list[Position] = []
        for pos in list(self.open_positions()):
            bar = (bars or {}).get(pos.symbol)
            mid = prices.get(pos.symbol)
            if mid is None and bar is not None:
                mid = bar.close
            if mid is None:
                continue
            # 只有在進場之後才開始的 K 棒,其高低點才算數(避免把進場前的波動當成觸價)
            use_bar = bar is not None and bar.time >= pos.opened_at
            if pos.direction is Direction.LONG:
                hit_stop = (bar.low <= pos.stop_loss) if use_bar else (mid <= pos.stop_loss)
                hit_tp = (bar.high >= pos.take_profit) if use_bar else (mid >= pos.take_profit)
            else:
                hit_stop = (bar.high >= pos.stop_loss) if use_bar else (mid >= pos.stop_loss)
                hit_tp = (bar.low <= pos.take_profit) if use_bar else (mid <= pos.take_profit)
            if hit_stop:
                closed.append(self.close_position(pos.position_id, pos.stop_loss, now, "停損"))
            elif hit_tp:
                closed.append(self.close_position(pos.position_id, pos.take_profit, now, "停利"))

        unrealized = 0.0
        for p in self.open_positions():
            mid = prices.get(p.symbol)
            if mid is None and bars and p.symbol in bars:
                mid = bars[p.symbol].close
            if mid is not None:
                unrealized += self._pnl(p, mid)
        self._account.equity = round(self._account.balance + unrealized, 2)
        self._account.peak_equity = max(self._account.peak_equity, self._account.equity)
        self._save()
        return closed
