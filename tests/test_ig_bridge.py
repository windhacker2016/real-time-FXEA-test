import json

import pytest

from fxea.ig_bridge import IGBroker, IGClient, IGError, IGFeed
from fxea.models import Direction, Timeframe
from fxea.state import StateStore

from .helpers import T0
from .test_risk import plan


class FakeIG:
    """記錄請求、回傳固定 JSON 的假 IG 伺服器。"""

    def __init__(self):
        self.calls = []
        self.logins = 0
        self.expire_once = False
        self.positions = []

    def __call__(self, method, url, headers, body):
        path = url.split("/gateway/deal", 1)[1]
        payload = json.loads(body) if body else None
        self.calls.append((method, path, headers, payload))
        if path == "/session":
            self.logins += 1
            return 200, {"CST": f"cst{self.logins}", "X-SECURITY-TOKEN": f"xst{self.logins}"}, json.dumps({"currentAccountId": "ABC", "accounts": [{"accountId": "ABC"}]}).encode()
        if self.expire_once:
            self.expire_once = False
            return 401, {}, b'{"errorCode":"error.security.oauth-token-invalid"}'
        if path.startswith("/prices/"):
            n = int(dict(p.split("=") for p in url.split("?")[1].split("&"))["max"])
            # 真實 IG 回傳「最新 n 根」:這裡以 24 根固定序列的尾端模擬
            prices = [
                {
                    "snapshotTimeUTC": f"2026-03-02T{i:02d}:00:00",
                    "openPrice": {"bid": 1.0840 + i * 1e-4, "ask": 1.0842 + i * 1e-4},
                    "highPrice": {"bid": 1.0850 + i * 1e-4, "ask": 1.0852 + i * 1e-4},
                    "lowPrice": {"bid": 1.0830 + i * 1e-4, "ask": 1.0832 + i * 1e-4},
                    "closePrice": {"bid": 1.0845 + i * 1e-4, "ask": 1.0847 + i * 1e-4},
                    "lastTradedVolume": 100 + i,
                }
                for i in range(24 - n, 24)
            ]
            return 200, {}, json.dumps({"prices": prices}).encode()
        if path.startswith("/markets/"):
            return 200, {}, json.dumps({"snapshot": {"bid": 1.0850, "offer": 1.0852}}).encode()
        if path == "/accounts":
            return 200, {}, json.dumps({"accounts": [{"accountId": "ABC", "currency": "USD", "balance": {"balance": 10000.0, "profitLoss": 25.0}}]}).encode()
        if path == "/positions" and method == "GET":
            return 200, {}, json.dumps({"positions": self.positions}).encode()
        if path == "/positions/otc" and method == "POST":
            if headers.get("_method") == "DELETE":
                self.positions = [p for p in self.positions if p["position"]["dealId"] != payload["dealId"]]
                return 200, {}, b'{"dealReference":"REF-CLOSE"}'
            self.positions.append(
                {
                    "position": {"dealId": "DEAL1", "direction": payload["direction"], "size": payload["size"], "level": 1.0851, "stopLevel": payload.get("stopLevel"), "limitLevel": payload.get("limitLevel"), "createdDateUTC": "2026-03-02T10:00:00"},
                    "market": {"epic": payload["epic"]},
                }
            )
            return 200, {}, b'{"dealReference":"REF-OPEN"}'
        if path.startswith("/confirms/"):
            ref = path.rsplit("/", 1)[1]
            return 200, {}, json.dumps({"dealStatus": "ACCEPTED", "dealId": "DEAL1", "level": 1.0851, "reason": "SUCCESS", "profit": 12.5 if ref == "REF-CLOSE" else None}).encode()
        return 404, {}, b"{}"


@pytest.fixture
def ig():
    fake = FakeIG()
    client = IGClient("key", "user", "pw", demo=True, transport=fake)
    client.login()
    return fake, client


def test_login_sets_tokens_and_headers(ig):
    fake, client = ig
    assert client.cst == "cst1" and client.xst == "xst1" and client.account_id == "ABC"
    client.account_list()
    method, path, headers, _ = fake.calls[-1]
    assert headers["CST"] == "cst1" and headers["X-SECURITY-TOKEN"] == "xst1" and headers["VERSION"] == "1"
    assert headers["X-IG-API-KEY"] == "key"


def test_relogin_on_401(ig):
    fake, client = ig
    fake.expire_once = True
    client.account_list()
    assert fake.logins == 2 and client.cst == "cst2"


def test_feed_candles_and_incremental_cache(ig):
    fake, client = ig
    feed = IGFeed(client, {"EURUSD": "CS.D.EURUSD.MINI.IP"}, incremental=2)
    df = feed.candles("EURUSD", Timeframe.H1, 5)
    assert list(df.columns) == ["time", "open", "high", "low", "close", "volume"] and len(df) == 5
    assert abs(df["close"].iloc[0] - (1.0846 + 19e-4)) < 1e-9  # 中價,第 19 小時
    assert [t.hour for t in df["time"]] == [19, 20, 21, 22, 23]
    n_calls = len(fake.calls)
    df2 = feed.candles("EURUSD", Timeframe.H1, 5)
    assert [t.hour for t in df2["time"]] == [19, 20, 21, 22, 23]
    assert len(fake.calls) == n_calls + 1 and "max=2" in fake.calls[-1][1]  # 只增量抓 2 根
    assert feed.price("EURUSD") == 1.0851
    with pytest.raises(KeyError):
        feed.epic("XAUUSD")


def test_broker_open_and_close(tmp_path, ig):
    fake, client = ig
    broker = IGBroker(client, {"EURUSD": "CS.D.EURUSD.MINI.IP"}, StateStore(tmp_path), T0)
    acct = broker.account()
    assert acct.balance == 10000.0 and acct.equity == 10025.0 and acct.currency == "USD"

    pos = broker.open_position(plan(), 1.5, 1.0850, T0, memo_id="FD-001-0302", risk_amount=75.0)
    method, path, headers, payload = next(c for c in fake.calls if c[1] == "/positions/otc")
    assert headers["VERSION"] == "2" and payload["direction"] == "BUY" and payload["size"] == 1.5
    assert payload["epic"] == "CS.D.EURUSD.MINI.IP" and payload["stopLevel"] == 1.08 and payload["limitLevel"] == 1.096
    assert payload["orderType"] == "MARKET" and payload["forceOpen"] is True
    assert pos.position_id == "DEAL1" and pos.entry_price == 1.0851

    live = broker.open_positions()
    assert live[0].memo_id == "FD-001-0302" and live[0].direction is Direction.LONG and live[0].risk_amount == 75.0
    assert broker.mark_to_market({"EURUSD": 1.086}, T0) == []

    closed = broker.close_position("DEAL1", 1.0860, T0, "手動")
    _, _, headers, payload = fake.calls[-2]
    assert headers["_method"] == "DELETE" and payload["direction"] == "SELL" and payload["dealId"] == "DEAL1"
    assert closed.status == "closed" and closed.pnl == 12.5
    assert broker.open_positions() == []


def test_broker_detects_server_side_close(tmp_path, ig):
    fake, client = ig
    broker = IGBroker(client, {"EURUSD": "CS.D.EURUSD.MINI.IP"}, StateStore(tmp_path), T0)
    broker.open_position(plan(), 1.0, 1.0850, T0, memo_id="FD-002-0302", risk_amount=50.0)
    fake.positions = []  # IG 端停損觸發
    closed = broker.mark_to_market({}, T0)
    assert [c.memo_id for c in closed] == ["FD-002-0302"] and "IG 端" in closed[0].close_reason


def test_rejected_deal_raises(tmp_path, ig):
    fake, client = ig
    original = fake.__call__

    def reject(method, url, headers, body):
        if "/confirms/" in url:
            return 200, {}, b'{"dealStatus":"REJECTED","reason":"INSUFFICIENT_FUNDS"}'
        return original(method, url, headers, body)

    client.transport = reject
    broker = IGBroker(client, {"EURUSD": "CS.D.EURUSD.MINI.IP"}, StateStore(tmp_path), T0)
    with pytest.raises(IGError):
        broker.open_position(plan(), 1.0, 1.0850, T0)
