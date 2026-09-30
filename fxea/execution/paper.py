"""紙上交易券商:在本機模擬成交、停損/停利與權益曲線。"""
from __future__ import annotations

from datetime import datetime

from ..instruments import get_instrument
from ..models import Account, Direction, Position, TradePlan
from ..state import StateStore
from .base import Broker


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, state: StateStore, starting_balance: float, currency: str, now: datetime):
        self.state = state
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

    def _pnl(self, pos: Position, price: float) -> float:
        inst = get_instrument(pos.symbol)
        pips = (price - pos.entry_price) * pos.direction.sign / inst.pip_size
        return pips * inst.pip_value(price, self._account.currency) * pos.lots

    def _save(self) -> None:
        self.state.save_account(self._account)
        self.state.save_positions(self._positions)

    # ------------------------------------------------------------------
    def open_position(self, plan: TradePlan, lots: float, price: float, now: datetime, memo_id=None, risk_amount=0.0) -> Position:
        seq = self.state.next_seq("position")
        pos = Position(
            position_id=f"P-{seq:04d}",
            symbol=plan.symbol,
            direction=plan.direction,
            lots=lots,
            entry_price=price,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            opened_at=now,
            memo_id=memo_id,
            risk_amount=risk_amount,
        )
        self._positions.append(pos)
        self._save()
        self.state.journal({"type": "open", "position_id": pos.position_id, "symbol": pos.symbol, "direction": pos.direction.value, "lots": lots, "price": price})
        return pos

    def close_position(self, position_id: str, price: float, now: datetime, reason: str) -> Position:
        pos = next((p for p in self._positions if p.position_id == position_id and p.status == "open"), None)
        if pos is None:
            raise KeyError(f"沒有未平倉部位 {position_id}")
        pnl = round(self._pnl(pos, price), 2)
        pos.status = "closed"
        pos.closed_at = now
        pos.close_price = price
        pos.pnl = pnl
        pos.close_reason = reason
        self._account.balance = round(self._account.balance + pnl, 2)
        self._account.daily_pnl = round(self._account.daily_pnl + pnl, 2)
        self._save()
        self.state.journal({"type": "close", "position_id": pos.position_id, "symbol": pos.symbol, "price": price, "pnl": pnl, "reason": reason})
        return pos

    def mark_to_market(self, prices: dict[str, float], now: datetime) -> list[Position]:
        if now.date() != self._account.day:
            self._account.day = now.date()
            self._account.daily_pnl = 0.0

        closed: list[Position] = []
        for pos in list(self.open_positions()):
            price = prices.get(pos.symbol)
            if price is None:
                continue
            if pos.direction is Direction.LONG:
                if price <= pos.stop_loss:
                    closed.append(self.close_position(pos.position_id, pos.stop_loss, now, "停損"))
                elif price >= pos.take_profit:
                    closed.append(self.close_position(pos.position_id, pos.take_profit, now, "停利"))
            else:
                if price >= pos.stop_loss:
                    closed.append(self.close_position(pos.position_id, pos.stop_loss, now, "停損"))
                elif price <= pos.take_profit:
                    closed.append(self.close_position(pos.position_id, pos.take_profit, now, "停利"))

        unrealized = sum(self._pnl(p, prices[p.symbol]) for p in self.open_positions() if p.symbol in prices)
        self._account.equity = round(self._account.balance + unrealized, 2)
        self._account.peak_equity = max(self._account.peak_equity, self._account.equity)
        self._save()
        return closed
