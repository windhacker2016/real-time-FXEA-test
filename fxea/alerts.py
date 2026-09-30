"""圖 6「觸發警示」:警示輸出到主控台、JSONL 檔案、Webhook。"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .config import AlertConfig
from .models import Alert, AlertLevel

log = logging.getLogger("fxea.alerts")

_ICON = {AlertLevel.INFO: "ℹ", AlertLevel.WARNING: "⚠", AlertLevel.CRITICAL: "‼"}


class AlertSink(ABC):
    @abstractmethod
    def emit(self, alert: Alert) -> None: ...


class ConsoleAlertSink(AlertSink):
    def emit(self, alert: Alert) -> None:
        sym = f"[{alert.symbol}] " if alert.symbol else ""
        print(f"{alert.time:%Y-%m-%d %H:%M} {_ICON[alert.level]} {sym}{alert.title} — {alert.message}")


class FileAlertSink(AlertSink):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, alert: Alert) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(alert.model_dump_json() + "\n")


class WebhookAlertSink(AlertSink):
    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    def emit(self, alert: Alert) -> None:
        body = json.dumps(alert.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except (urllib.error.URLError, OSError) as exc:  # 警示失敗不能讓循環停止
            log.warning("Webhook 警示送出失敗:%s", exc)


class MemoryAlertSink(AlertSink):
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def emit(self, alert: Alert) -> None:
        self.alerts.append(alert)


class CompositeAlertSink(AlertSink):
    def __init__(self, sinks: list[AlertSink]):
        self.sinks = sinks

    def emit(self, alert: Alert) -> None:
        for s in self.sinks:
            try:
                s.emit(alert)
            except Exception as exc:  # noqa: BLE001
                log.warning("警示輸出 %s 失敗:%s", type(s).__name__, exc)


def build_sinks(cfg: AlertConfig, state_dir: str | Path) -> AlertSink:
    sinks: list[AlertSink] = []
    if cfg.console:
        sinks.append(ConsoleAlertSink())
    if cfg.file:
        sinks.append(FileAlertSink(Path(state_dir) / cfg.file))
    if cfg.webhook_url:
        sinks.append(WebhookAlertSink(cfg.webhook_url))
    return CompositeAlertSink(sinks)
