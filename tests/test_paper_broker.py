from datetime import timedelta

from fxea.execution.paper import PaperBroker
from fxea.state import StateStore

from .helpers import T0
from .test_risk import plan


def test_open_take_profit_and_persistence(tmp_path):
    store = StateStore(tmp_path)
    broker = PaperBroker(store, 10_000.0, "USD", T0)
    pos = broker.open_position(plan(), 0.2, 1.0850, T0, memo_id="FD-001-0302", risk_amount=100.0)
    assert pos.position_id == "P-0001" and broker.open_positions()

    assert broker.mark_to_market({"EURUSD": 1.0900}, T0 + timedelta(hours=1)) == []
    assert broker.account().equity == 10_100.0  # 50 點 × 10 × 0.2

    closed = broker.mark_to_market({"EURUSD": 1.0970}, T0 + timedelta(hours=2))
    assert [c.close_reason for c in closed] == ["停利"]
    assert closed[0].pnl == 220.0 and broker.account().balance == 10_220.0
    assert broker.account().daily_pnl == 220.0

    # 重新載入狀態
    again = PaperBroker(store, 10_000.0, "USD", T0)
    assert again.account().balance == 10_220.0 and again.open_positions() == []
    assert again.all_positions()[0].status == "closed"


def test_stop_loss_drawdown_and_day_rollover(tmp_path):
    broker = PaperBroker(StateStore(tmp_path), 10_000.0, "USD", T0)
    broker.open_position(plan(), 0.2, 1.0850, T0, risk_amount=100.0)
    closed = broker.mark_to_market({"EURUSD": 1.0790}, T0 + timedelta(hours=1))
    assert closed[0].close_reason == "停損" and closed[0].pnl == -100.0
    acct = broker.account()
    assert acct.balance == 9_900.0 and abs(acct.drawdown_pct - 1.0) < 1e-6 and acct.daily_pnl == -100.0

    broker.mark_to_market({}, T0 + timedelta(days=1))
    assert broker.account().daily_pnl == 0.0
