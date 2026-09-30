"""IG(IG Markets)REST API 橋接:即時 K 棒、報價、帳戶、下單(備用通道)。

主要即時通道建議用 IG 提供的 MT5(:mod:`fxea.mt5_bridge`);
此 REST 橋接適合部署在沒有 MT5 終端的 Linux 主機。
注意 IG REST 有歷史價格配額(約每週 10,000 個資料點),
所以 :class:`IGFeed` 會快取 K 棒、只增量抓最新幾根。

環境變數:``IG_API_KEY``、``IG_IDENTIFIER``、``IG_PASSWORD``;``ig.demo`` 決定模擬或真實。
只用標準函式庫(urllib),``transport`` 可注入以便離線測試。

注意:此橋接依 IG 公開 API 文件撰寫,尚未在本開發環境對 IG 模擬帳戶實測;
上線前請先以 ``ig.demo: true`` 跑通完整流程。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from .config import IGConfig
from .data.base import MarketDataFeed
from .execution.base import Broker
from .indicators import COLUMNS
from .models import Account, Direction, Position, Timeframe, TradePlan
from .state import StateStore

log = logging.getLogger("fxea.ig")

DEMO_URL = "https://demo-api.ig.com/gateway/deal"
LIVE_URL = "https://api.ig.com/gateway/deal"

_RESOLUTION = {Timeframe.M15: "MINUTE_15", Timeframe.H1: "HOUR", Timeframe.H4: "HOUR_4", Timeframe.D1: "DAY"}

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class IGError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"IG API {status}: {message}")
        self.status = status


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, {k.upper(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.upper(): v for k, v in exc.headers.items()}, exc.read()


class IGClient:
    def __init__(
        self,
        api_key: str,
        identifier: str,
        password: str,
        demo: bool = True,
        transport: Transport | None = None,
    ):
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.base = DEMO_URL if demo else LIVE_URL
        self.transport = transport or _urllib_transport
        self.cst: str | None = None
        self.xst: str | None = None
        self.account_id: str | None = None
        self.accounts: list[dict[str, Any]] = []

    @classmethod
    def from_env(cls, cfg: IGConfig, transport: Transport | None = None) -> "IGClient":
        key, ident, pwd = os.environ.get("IG_API_KEY"), os.environ.get("IG_IDENTIFIER"), os.environ.get("IG_PASSWORD")
        if not (key and ident and pwd):
            raise RuntimeError("請設定環境變數 IG_API_KEY / IG_IDENTIFIER / IG_PASSWORD")
        client = cls(key, ident, pwd, demo=cfg.demo, transport=transport)
        client.login()
        if cfg.account_id:
            client.account_id = cfg.account_id
        return client

    # ------------------------------------------------------------------
    def _headers(self, version: int, method_override: str | None = None) -> dict[str, str]:
        h = {
            "X-IG-API-KEY": self.api_key,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "VERSION": str(version),
        }
        if self.cst and self.xst:
            h["CST"] = self.cst
            h["X-SECURITY-TOKEN"] = self.xst
        if method_override:
            h["_method"] = method_override
        return h

    def login(self) -> dict[str, Any]:
        body = json.dumps({"identifier": self.identifier, "password": self.password}).encode("utf-8")
        status, headers, raw = self.transport("POST", f"{self.base}/session", self._headers(2), body)
        if status >= 300:
            raise IGError(status, raw.decode("utf-8", "replace"))
        self.cst = headers.get("CST")
        self.xst = headers.get("X-SECURITY-TOKEN")
        data = json.loads(raw or b"{}")
        self.account_id = data.get("currentAccountId")
        self.accounts = data.get("accounts", [])
        return data

    def request(
        self,
        method: str,
        path: str,
        version: int,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        method_override: str | None = None,
        _retry: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        status, _, raw = self.transport(method, url, self._headers(version, method_override), payload)
        if status == 401 and _retry:
            self.login()
            return self.request(method, path, version, body, params, method_override, _retry=False)
        if status >= 300:
            raise IGError(status, raw.decode("utf-8", "replace"))
        return json.loads(raw) if raw else {}

    # ---- 行情 ----------------------------------------------------------
    def prices(self, epic: str, resolution: str, n: int) -> list[dict[str, Any]]:
        data = self.request("GET", f"/prices/{epic}", 3, params={"resolution": resolution, "max": n, "pageSize": n})
        return data.get("prices", [])

    def market(self, epic: str) -> dict[str, Any]:
        return self.request("GET", f"/markets/{epic}", 3)

    # ---- 帳戶 / 部位 ---------------------------------------------------
    def account_list(self) -> list[dict[str, Any]]:
        return self.request("GET", "/accounts", 1).get("accounts", [])

    def position_list(self) -> list[dict[str, Any]]:
        return self.request("GET", "/positions", 2).get("positions", [])

    def confirm(self, deal_reference: str) -> dict[str, Any]:
        return self.request("GET", f"/confirms/{deal_reference}", 1)

    def open_position(
        self,
        epic: str,
        direction: str,
        size: float,
        currency: str,
        stop_level: float | None,
        limit_level: float | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "epic": epic,
            "expiry": "-",
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
            "guaranteedStop": False,
            "forceOpen": True,
            "currencyCode": currency,
        }
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if limit_level is not None:
            body["limitLevel"] = limit_level
        ref = self.request("POST", "/positions/otc", 2, body=body).get("dealReference")
        return self.confirm(ref)

    def close_position(self, deal_id: str, direction: str, size: float) -> dict[str, Any]:
        body = {"dealId": deal_id, "direction": direction, "size": size, "orderType": "MARKET", "timeInForce": "FILL_OR_KILL"}
        ref = self.request("POST", "/positions/otc", 1, body=body, method_override="DELETE").get("dealReference")
        return self.confirm(ref)


# ---------------------------------------------------------------------------
# 行情來源
# ---------------------------------------------------------------------------


def _mid(p: dict[str, Any]) -> float:
    bid, ask = p.get("bid"), p.get("ask")
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2.0
    return float(bid if bid is not None else ask)


def _parse_time(s: str) -> datetime:
    s = s.replace("/", "-").replace(" ", "T")
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class IGFeed(MarketDataFeed):
    """快取 K 棒、只增量抓最新幾根,節省 IG 的歷史價格配額。"""

    def __init__(self, client: IGClient, epics: dict[str, str], incremental: int = 3):
        self.client = client
        self.epics = {k.upper(): v for k, v in epics.items()}
        self.incremental = incremental
        self._cache: dict[tuple[str, Timeframe], pd.DataFrame] = {}

    def epic(self, symbol: str) -> str:
        try:
            return self.epics[symbol.upper()]
        except KeyError as exc:
            raise KeyError(f"未設定 {symbol} 的 IG epic(ig.epics)") from exc

    @staticmethod
    def to_frame(prices: list[dict[str, Any]]) -> pd.DataFrame:
        rows = []
        for p in prices:
            rows.append(
                {
                    "time": _parse_time(p.get("snapshotTimeUTC") or p["snapshotTime"]),
                    "open": _mid(p["openPrice"]),
                    "high": _mid(p["highPrice"]),
                    "low": _mid(p["lowPrice"]),
                    "close": _mid(p["closePrice"]),
                    "volume": float(p.get("lastTradedVolume") or 0.0),
                }
            )
        return pd.DataFrame(rows, columns=COLUMNS)

    def candles(self, symbol: str, timeframe: Timeframe, n: int) -> pd.DataFrame:
        key = (symbol.upper(), timeframe)
        res = _RESOLUTION[timeframe]
        cached = self._cache.get(key)
        if cached is None or len(cached) < n:
            df = self.to_frame(self.client.prices(self.epic(symbol), res, n))
        else:
            fresh = self.to_frame(self.client.prices(self.epic(symbol), res, self.incremental))
            if fresh.empty:
                df = cached
            else:
                first = fresh["time"].iloc[0]
                df = pd.concat([cached[cached["time"] < first], fresh], ignore_index=True).tail(max(n, len(cached)))
        self._cache[key] = df.reset_index(drop=True)
        return self._cache[key].tail(n).reset_index(drop=True)

    def price(self, symbol: str) -> float:
        snap = self.client.market(self.epic(symbol)).get("snapshot", {})
        return _mid({"bid": snap.get("bid"), "ask": snap.get("offer")})

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 券商
# ---------------------------------------------------------------------------


class IGBroker(Broker):
    """停損/停利交由 IG 伺服器端執行;本地只追蹤 dealId ↔ 備忘錄對應與日內損益。"""

    name = "ig"

    def __init__(self, client: IGClient, epics: dict[str, str], state: StateStore, now: datetime):
        self.client = client
        self.epics = {k.upper(): v for k, v in epics.items()}
        self.symbols = {v: k for k, v in self.epics.items()}
        self.state = state
        meta = state.read_json("ig_meta.json") or {}
        self._tracked: dict[str, dict[str, Any]] = meta.get("tracked", {})
        self._day = meta.get("day")
        self._day_start_equity = meta.get("day_start_equity")
        self._peak = meta.get("peak_equity")
        self._account_cache: Account | None = None
        self._rollover(now)

    def _save_meta(self) -> None:
        self.state.write_json(
            "ig_meta.json",
            {"tracked": self._tracked, "day": self._day, "day_start_equity": self._day_start_equity, "peak_equity": self._peak},
        )

    def _rollover(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._day != today:
            self._day = today
            self._day_start_equity = None
            self._save_meta()

    def _raw_account(self) -> dict[str, Any]:
        accounts = self.client.account_list()
        for a in accounts:
            if a.get("accountId") == self.client.account_id:
                return a
        if accounts:
            return accounts[0]
        raise IGError(404, "IG 沒有回傳任何帳戶")

    def account(self) -> Account:
        raw = self._raw_account()
        bal = raw.get("balance", {})
        balance = float(bal.get("balance", 0.0))
        equity = balance + float(bal.get("profitLoss", 0.0) or 0.0)
        if self._day_start_equity is None:
            self._day_start_equity = equity
        self._peak = max(self._peak or equity, equity)
        self._save_meta()
        self._account_cache = Account(
            currency=raw.get("currency", "USD"),
            balance=balance,
            equity=equity,
            peak_equity=self._peak,
            day=datetime.fromisoformat(self._day).date(),
            daily_pnl=round(equity - self._day_start_equity, 2),
        )
        return self._account_cache

    def _to_position(self, item: dict[str, Any]) -> Position:
        p, m = item["position"], item["market"]
        deal_id = p["dealId"]
        meta = self._tracked.get(deal_id, {})
        opened = p.get("createdDateUTC") or p.get("createdDate")
        return Position(
            position_id=deal_id,
            symbol=self.symbols.get(m["epic"], m["epic"]),
            direction=Direction.LONG if p["direction"] == "BUY" else Direction.SHORT,
            lots=float(p["size"]),
            entry_price=float(p["level"]),
            stop_loss=float(p.get("stopLevel") or 0.0),
            take_profit=float(p.get("limitLevel") or 0.0),
            opened_at=_parse_time(opened) if opened else datetime.now(timezone.utc),
            memo_id=meta.get("memo_id"),
            risk_amount=float(meta.get("risk_amount", 0.0)),
        )

    def open_positions(self) -> list[Position]:
        return [self._to_position(item) for item in self.client.position_list()]

    def open_position(self, plan: TradePlan, lots: float, price: float, now: datetime, memo_id=None, risk_amount=0.0) -> Position:
        epic = self.epics[plan.symbol.upper()]
        currency = (self._account_cache.currency if self._account_cache else None) or self.account().currency
        confirm = self.client.open_position(
            epic=epic,
            direction="BUY" if plan.direction is Direction.LONG else "SELL",
            size=lots,
            currency=currency,
            stop_level=plan.stop_loss,
            limit_level=plan.take_profit,
        )
        if confirm.get("dealStatus") != "ACCEPTED":
            raise IGError(400, f"下單被拒:{confirm.get('reason')}")
        deal_id = confirm["dealId"]
        self._tracked[deal_id] = {"memo_id": memo_id, "risk_amount": risk_amount, "symbol": plan.symbol}
        self._save_meta()
        self.state.journal({"type": "open", "broker": "ig", "position_id": deal_id, "symbol": plan.symbol, "lots": lots, "level": confirm.get("level")})
        return Position(
            position_id=deal_id,
            symbol=plan.symbol,
            direction=plan.direction,
            lots=lots,
            entry_price=float(confirm.get("level") or price),
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            opened_at=now,
            memo_id=memo_id,
            risk_amount=risk_amount,
        )

    def close_position(self, position_id: str, price: float, now: datetime, reason: str) -> Position:
        pos = next((p for p in self.open_positions() if p.position_id == position_id), None)
        if pos is None:
            raise KeyError(f"IG 沒有未平倉部位 {position_id}")
        confirm = self.client.close_position(position_id, "SELL" if pos.direction is Direction.LONG else "BUY", pos.lots)
        if confirm.get("dealStatus") != "ACCEPTED":
            raise IGError(400, f"平倉被拒:{confirm.get('reason')}")
        pos.status = "closed"
        pos.closed_at = now
        pos.close_price = float(confirm.get("level") or price)
        pos.pnl = float(confirm.get("profit") or 0.0)
        pos.close_reason = reason
        self._tracked.pop(position_id, None)
        self._save_meta()
        self.state.journal({"type": "close", "broker": "ig", "position_id": position_id, "reason": reason, "pnl": pos.pnl})
        return pos

    def mark_to_market(self, prices: dict[str, float], now: datetime) -> list[Position]:
        self._rollover(now)
        live = {p.position_id for p in self.open_positions()}
        closed: list[Position] = []
        for deal_id, meta in list(self._tracked.items()):
            if deal_id not in live:
                closed.append(
                    Position(
                        position_id=deal_id,
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
                        close_reason="IG 端已平倉(停損/停利或手動)",
                    )
                )
                self._tracked.pop(deal_id, None)
        if closed:
            self._save_meta()
        self.account()
        return closed
