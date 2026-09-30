from datetime import timedelta

from fxea.execution.paper import PaperBroker
from fxea.models import Candle
from fxea.state import StateStore

from .helpers import T0
from .test_risk import plan

H = timedelta(hours=1)


def test_open_take_profit_and_persistence(tmp_path):
    store = StateStore(tmp_path)
    broker = PaperBroker(store, 10_000.0, "USD", T0, spread_pips=0.0)
    pos = broker.open_position(plan(), 0.2, 1.0850, T0, memo_id="FD-001-0302", risk_amount=100.0)
    assert pos.position_id == "P-0001" and broker.open_positions()

    assert broker.mark_to_market({"EURUSD": 1.0900}, T0 + H) == []
    assert broker.account().equity == 10_100.0  # 50 點 × 10 × 0.2

    closed = broker.mark_to_market({"EURUSD": 1.0970}, T0 + 2 * H)
    assert [c.close_reason for c in closed] == ["停利"]
    assert closed[0].pnl == 220.0 and broker.account().balance == 10_220.0
    assert broker.account().daily_pnl == 220.0

    # 重新載入狀態
    again = PaperBroker(store, 10_000.0, "USD", T0, spread_pips=0.0)
    assert again.account().balance == 10_220.0 and again.open_positions() == []
    assert again.all_positions()[0].status == "closed"


def test_stop_loss_drawdown_and_day_rollover(tmp_path):
    broker = PaperBroker(StateStore(tmp_path), 10_000.0, "USD", T0, spread_pips=0.0)
    broker.open_position(plan(), 0.2, 1.0850, T0, risk_amount=100.0)
    closed = broker.mark_to_market({"EURUSD": 1.0790}, T0 + H)
    assert closed[0].close_reason == "停損" and closed[0].pnl == -100.0
    acct = broker.account()
    assert acct.balance == 9_900.0 and abs(acct.drawdown_pct - 1.0) < 1e-6 and acct.daily_pnl == -100.0

    broker.mark_to_market({}, T0 + timedelta(days=1))
    assert broker.account().daily_pnl == 0.0


def test_intrabar_stop_first_and_spread(tmp_path):
    broker = PaperBroker(StateStore(tmp_path), 10_000.0, "USD", T0, spread_pips=1.0)
    pos = broker.open_position(plan(), 0.2, 1.0850, T0, risk_amount=100.0)
    assert pos.entry_price == 1.08505  # 買在賣價(中價 + 半個點差)

    # 進場前就開始的 K 棒:高低點不算數,只看收盤(未觸及)
    early = Candle(time=T0 - H, open=1.0850, high=1.0860, low=1.0790, close=1.0855, volume=1)
    assert broker.mark_to_market({"EURUSD": 1.0855}, T0, {"EURUSD": early}) == []

    # 進場後的 K 棒同時觸及停損與停利 → 保守先算停損;成交再扣半個點差
    both = Candle(time=T0, open=1.0850, high=1.0970, low=1.0795, close=1.0900, volume=1)
    closed = broker.mark_to_market({"EURUSD": 1.0900}, T0 + H, {"EURUSD": both})
    assert closed[0].close_reason == "停損" and closed[0].close_price == 1.07995
    assert closed[0].pnl == -102.0  # (1.07995 − 1.08505) = −51 點 × 10 × 0.2


def test_intrabar_take_profit(tmp_path):
    broker = PaperBroker(StateStore(tmp_path), 10_000.0, "USD", T0, spread_pips=1.0)
    broker.open_position(plan(), 0.2, 1.0850, T0, risk_amount=100.0)
    bar = Candle(time=T0, open=1.0850, high=1.0965, low=1.0840, close=1.0930, volume=1)
    closed = broker.mark_to_market({"EURUSD": 1.0930}, T0 + H, {"EURUSD": bar})
    assert closed[0].close_reason == "停利" and closed[0].close_price == 1.09595
    assert closed[0].pnl == 218.0  # 109 點 × 10 × 0.2
