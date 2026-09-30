"""依設定組裝各元件(資料來源、券商、研究分析器、警示)。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .alerts import AlertSink, build_sinks
from .config import Settings
from .data import CsvFeed, MarketDataFeed, NewsFeed, StaticNewsFeed, SyntheticFeed
from .execution import Broker, PaperBroker
from .instruments import get_instrument, register
from .monitor import MonitorLoop
from .research import Analyst, ClaudeAnalyst, RuleBasedAnalyst
from .state import StateStore

log = logging.getLogger("fxea.app")


def apply_instrument_overrides(settings: Settings) -> None:
    for symbol, ov in settings.instruments.items():
        base = get_instrument(symbol)
        data = base.model_dump()
        data.update({k: v for k, v in ov.model_dump().items() if v is not None})
        register(type(base).model_validate(data))


def build_feed(settings: Settings, ig_client=None) -> MarketDataFeed:
    src = settings.data_source
    if src == "synthetic":
        return SyntheticFeed(settings.watchlist, seed=settings.synthetic_seed)
    if src == "csv":
        if not settings.csv_dir:
            raise ValueError("data_source=csv 需要設定 csv_dir")
        return CsvFeed(settings.csv_dir, settings.watchlist)
    if src == "mt5":
        from .mt5_bridge import MT5Feed

        return MT5Feed(settings.mt5.symbol_map)
    if src == "ig":
        from .ig_bridge import IGClient, IGFeed

        client = ig_client or IGClient.from_env(settings.ig)
        return IGFeed(client, settings.ig.epics)
    raise ValueError(f"未知的 data_source:{src}")


def build_news(settings: Settings) -> NewsFeed | None:
    if settings.news_file and Path(settings.news_file).exists():
        return StaticNewsFeed.from_file(settings.news_file)
    return None


def build_broker(settings: Settings, state: StateStore, now: datetime, ig_client=None) -> Broker:
    if settings.broker == "paper":
        return PaperBroker(state, settings.account.starting_balance, settings.account.currency, now)
    if settings.broker == "mt5":
        from .mt5_bridge import MT5Broker

        return MT5Broker(state, now, settings.mt5.magic, settings.mt5.deviation, settings.mt5.symbol_map)
    if settings.broker == "ig":
        from .ig_bridge import IGBroker, IGClient

        client = ig_client or IGClient.from_env(settings.ig)
        return IGBroker(client, settings.ig.epics, state, now)
    raise ValueError(f"未知的 broker:{settings.broker}")


def build_analyst(settings: Settings, client=None) -> Analyst:
    cfg = settings.research
    if cfg.provider == "claude":
        try:
            return ClaudeAnalyst(cfg, client=client)
        except Exception as exc:  # noqa: BLE001 - 無金鑰等情況退回規則式
            log.warning("無法建立 Claude 分析器(%s),改用規則式分析", exc)
    return RuleBasedAnalyst()


def build_alerts(settings: Settings) -> AlertSink:
    return build_sinks(settings.alerts, settings.state_dir)


def build_loop(settings: Settings, **overrides) -> MonitorLoop:
    """組裝完整監控循環;``overrides`` 可注入 feed/broker/analyst/alerts/state/news(測試用)。"""
    apply_instrument_overrides(settings)
    state = overrides.get("state") or StateStore(settings.state_dir)
    ig_client = None
    if "ig" in (settings.data_source, settings.broker) and not ({"feed", "broker"} <= overrides.keys()):
        from .ig_bridge import IGClient

        ig_client = overrides.get("ig_client") or IGClient.from_env(settings.ig)
    feed = overrides.get("feed") or build_feed(settings, ig_client)
    now = feed.now() if hasattr(feed, "now") else datetime.now(timezone.utc)
    broker = overrides.get("broker") or build_broker(settings, state, now, ig_client)
    analyst = overrides.get("analyst") or build_analyst(settings)
    alerts = overrides.get("alerts") or build_alerts(settings)
    news = overrides.get("news") if "news" in overrides else build_news(settings)
    return MonitorLoop(settings, feed, broker, analyst, alerts, state, news)
