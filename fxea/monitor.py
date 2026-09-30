"""圖 6 全天候監控循環:掃描 → 訊號 → 風險 → 監控 → 警示 → (回到掃描)。

外圈四個節點:自選清單更新、觸發警示、狀態監控、主動檢視。
「系統全天候上線 — 不會停機,沒有開閉市間隔」「監控循環啟用 — 持續監看,隨時待命」
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable

from .alerts import AlertSink
from .approval import ApprovalGate
from .config import Settings
from .data.base import MarketDataFeed, NewsFeed
from .execution.base import Broker
from .memo import MemoBuilder, render_memo
from .models import Alert, AlertLevel, CycleReport, Decision, DecisionMemo, MemoStatus, Position
from .planner import TradePlanner
from .research import Analyst, ResearchContext
from .risk import RiskModule
from .scanner import MarketScanner, ScanResult
from .signals import SignalEngine
from .state import StateStore

log = logging.getLogger("fxea.monitor")

_LIVE_SOURCES = {"mt5", "ig"}


class MonitorLoop:
    def __init__(
        self,
        settings: Settings,
        feed: MarketDataFeed,
        broker: Broker,
        analyst: Analyst,
        alerts: AlertSink,
        state: StateStore,
        news: NewsFeed | None = None,
    ):
        self.settings = settings
        self.feed = feed
        self.broker = broker
        self.analyst = analyst
        self.alerts = alerts
        self.state = state
        self.scanner = MarketScanner(feed, settings.scanner, news)
        self.engine = SignalEngine(settings.signals)
        self.planner = TradePlanner(settings.planner)
        self.risk = RiskModule(settings.risk)
        self.gate = ApprovalGate(state, settings.approval)
        self.memos = MemoBuilder(state)
        self.base_watchlist = [s.upper() for s in settings.watchlist]
        self.watchlist: list[str] = list(self.base_watchlist)
        self.replay = settings.data_source not in _LIVE_SOURCES
        self.cycle = 0
        self.started_at: datetime | None = None
        self.last_report: CycleReport | None = None
        self._recent_blocks: dict[tuple[str, str], datetime] = {}

    # ------------------------------------------------------------------
    def emit(self, level: AlertLevel, title: str, message: str, symbol: str | None = None, report: CycleReport | None = None) -> Alert:
        alert = Alert(time=self.feed.now(), level=level, title=title, message=message, symbol=symbol)
        self.alerts.emit(alert)
        if report is not None:
            report.alerts.append(alert)
        return alert

    # ---- 自選清單更新 ----------------------------------------------------
    def refresh_watchlist(self) -> list[str]:
        merged = list(self.base_watchlist)
        for sym in self.gate.active_symbols() | {p.symbol for p in self.broker.open_positions()}:
            if sym not in merged:
                merged.append(sym)
        self.watchlist = merged
        return merged

    # ---- 監控:既有部位 / 待審備忘錄 ----------------------------------------
    def _monitor_positions(self, prices: dict[str, float], now: datetime, report: CycleReport) -> None:
        for pos in self.broker.mark_to_market(prices, now):
            report.closed.append(pos.position_id)
            level = AlertLevel.WARNING if pos.pnl < 0 else AlertLevel.INFO
            self.emit(level, f"部位平倉:{pos.close_reason}", f"{pos.position_id} {pos.direction.label_zh} {pos.lots} 手 @ {pos.close_price} 損益 {pos.pnl:+.2f}", pos.symbol, report)
            if pos.memo_id:
                try:
                    memo = self.state.load_memo(pos.memo_id)
                    memo.note = f"部位 {pos.position_id} 已平倉:{pos.close_reason},損益 {pos.pnl:+.2f}"
                    self.state.save_memo(memo)
                except KeyError:
                    pass

    def _monitor_memos(self, prices: dict[str, float], now: datetime, report: CycleReport) -> None:
        for memo in self.gate.expire_stale(now):
            self.emit(AlertLevel.INFO, "備忘錄逾時失效", f"{memo.memo_id} 超過等待時限未審核", memo.symbol, report)
        for memo in self.gate.pending() + self.gate.watchlist() + self.gate.approved_unexecuted():
            price = prices.get(memo.symbol)
            if price is None:
                continue
            if memo.plan.is_invalidated(price):
                self.gate.expire(memo, f"失效條件觸發:價格 {price} 已越過 {memo.plan.invalidation_level}", now)
                self.emit(AlertLevel.WARNING, "交易設定失效", f"{memo.memo_id} 價格 {price} 突破失效價位 {memo.plan.invalidation_level}", memo.symbol, report)
            elif memo.decision is Decision.WATCHLIST and memo.plan.price_in_entry_zone(price):
                self.emit(AlertLevel.WARNING, "觀察清單條件達成", f"{memo.memo_id} 價格 {price} 進入進場區 {memo.plan.entry_zone_low}–{memo.plan.entry_zone_high},可執行 fxea decide {memo.memo_id} approve", memo.symbol, report)

    # ---- 執行已核准 --------------------------------------------------------
    def _execute_approved(self, prices: dict[str, float], now: datetime, report: CycleReport) -> None:
        for memo in self.gate.approved_unexecuted():
            price = prices.get(memo.symbol)
            if price is None:
                try:
                    price = self.feed.price(memo.symbol)
                except Exception as exc:  # noqa: BLE001
                    self.emit(AlertLevel.CRITICAL, "無法取得報價", f"{memo.memo_id} {exc}", memo.symbol, report)
                    continue
            plan = memo.plan
            if plan.is_invalidated(price):
                self.gate.expire(memo, f"執行前失效:價格 {price} 已越過 {plan.invalidation_level}", now)
                self.emit(AlertLevel.WARNING, "核准後失效,未執行", f"{memo.memo_id} 價格 {price}", memo.symbol, report)
                continue
            if abs(price - plan.entry_price) > 1.0 * plan.atr:
                self.gate.expire(memo, f"價格 {price} 已離開進場區(距進場價超過 1 ATR),未執行", now)
                self.emit(AlertLevel.WARNING, "價格離開進場區,未執行", f"{memo.memo_id} 現價 {price} 進場 {plan.entry_price}", memo.symbol, report)
                continue
            try:
                pos = self.broker.open_position(plan, memo.risk.lots, price, now, memo.memo_id, memo.risk.risk_amount)
            except Exception as exc:  # noqa: BLE001
                self.gate.expire(memo, f"下單失敗:{exc}", now)
                self.emit(AlertLevel.CRITICAL, "下單失敗", f"{memo.memo_id} {exc}", memo.symbol, report)
                continue
            memo.executed = True
            memo.position_id = pos.position_id
            self.state.save_memo(memo)
            report.executed.append(memo.memo_id)
            self.emit(
                AlertLevel.WARNING,
                "已執行交易",
                f"{memo.memo_id} → {pos.position_id} {plan.direction.label_zh} {pos.lots} 手 @ {pos.entry_price} 停損 {plan.stop_loss} 停利 {plan.take_profit}",
                memo.symbol,
                report,
            )

    # ---- 訊號 → 計畫 → 風險 → 研究 → 備忘錄 -----------------------------------
    def _process_opportunity(self, res: ScanResult, now: datetime, report: CycleReport) -> DecisionMemo | None:
        snapshot = res.snapshot
        signals = self.engine.detect(snapshot, res.candles)
        if not signals:
            return None
        report.signals += len(signals)
        plan = self.planner.build(signals[0], snapshot, res.candles)
        account = self.broker.account()
        open_positions = self.broker.open_positions()
        risk = self.risk.evaluate(plan, snapshot, account, open_positions, now)

        # 同商品、同樣的阻擋原因在冷卻期內只記錄一次(避免洗版,也省下研究層呼叫)
        if not (risk.passed and plan.valid):
            failed = ",".join(c.name for c in risk.checks if not c.passed) or "plan_invalid"
            key = (snapshot.symbol, failed)
            last = self._recent_blocks.get(key)
            cooldown = timedelta(minutes=self.settings.monitor.block_cooldown_minutes)
            if last is not None and now - last < cooldown:
                return None
            self._recent_blocks[key] = now

        research = self.analyst.assess(ResearchContext(snapshot, signals, plan, risk, open_positions))
        memo = self.memos.build(snapshot, signals, plan, risk, research, now)
        memo = self.gate.submit(memo, now)

        sig = signals[0]
        if memo.status is MemoStatus.READY:
            report.memos_created.append(memo.memo_id)
            how = "自動核准,將執行" if memo.decision is Decision.APPROVE else "需要人工核准"
            self.emit(
                AlertLevel.WARNING,
                f"交易計畫完成 — {how}",
                f"{memo.memo_id} {sig.signal_type.label_zh} {plan.direction.label_zh} 評分 {sig.score:.0f} "
                f"進場 {plan.entry_price} 停損 {plan.stop_loss} 停利 {plan.take_profit} R:R 1:{plan.risk_reward:.2f} "
                f"信心 {research.stars} 研究建議:{research.recommendation_zh}",
                snapshot.symbol,
                report,
            )
        else:
            report.memos_blocked.append(memo.memo_id)
            self.emit(AlertLevel.INFO, "風險模組阻擋", f"{memo.memo_id} {sig.signal_type.label_zh} {plan.direction.label_zh}:{'; '.join(risk.blocked_reasons) or plan.invalid_reason}", snapshot.symbol, report)
        return memo

    # ---- 狀態監控 / 主動檢視 ------------------------------------------------
    def _status(self, now: datetime, report: CycleReport, announce: bool) -> None:
        account = self.broker.account()
        open_positions = self.broker.open_positions()
        report.equity = account.equity
        report.drawdown_pct = round(account.drawdown_pct, 3)
        report.open_positions = len(open_positions)
        report.pending_memos = len(self.gate.pending())
        uptime = (now - self.started_at) if self.started_at else timedelta(0)
        status = {
            "time": now.isoformat(),
            "cycle": self.cycle,
            "uptime_seconds": int(uptime.total_seconds()),
            "watchlist": self.watchlist,
            "equity": account.equity,
            "balance": account.balance,
            "drawdown_pct": report.drawdown_pct,
            "daily_pnl": account.daily_pnl,
            "open_positions": [p.model_dump(mode="json") for p in open_positions],
            "pending_memos": [m.memo_id for m in self.gate.pending()],
            "scanner_errors": self.scanner.errors,
            "system": "online",
        }
        self.state.save_status(status)
        if announce:
            self.emit(
                AlertLevel.INFO,
                "狀態監控",
                f"第 {self.cycle} 輪 | 權益 {account.equity:.2f} | 回撤 {account.drawdown_pct:.2f}% | 今日 {account.daily_pnl:+.2f} | 未平倉 {len(open_positions)} | 待審 {report.pending_memos} | 系統運作正常",
                None,
                report,
            )

    def _proactive_review(self, prices: dict[str, float], now: datetime, report: CycleReport) -> None:
        for pos in self.broker.open_positions():
            price = prices.get(pos.symbol)
            if price is None or pos.risk_amount <= 0 or pos.stop_loss == 0:
                continue
            r_dist = abs(pos.entry_price - pos.stop_loss)
            if r_dist <= 0:
                continue
            r_multiple = (price - pos.entry_price) * pos.direction.sign / r_dist
            age_h = (now - pos.opened_at).total_seconds() / 3600.0
            if r_multiple >= 1.0:
                self.emit(AlertLevel.INFO, "主動檢視:部位獲利 ≥ 1R", f"{pos.position_id} 目前 {r_multiple:+.2f}R,持有 {age_h:.0f} 小時,可考慮移動停損至損益兩平", pos.symbol, report)
            elif age_h >= 48 and abs(r_multiple) < 0.3:
                self.emit(AlertLevel.INFO, "主動檢視:部位停滯", f"{pos.position_id} 持有 {age_h:.0f} 小時仍在 {r_multiple:+.2f}R,請重新評估", pos.symbol, report)
        for memo in self.gate.pending():
            age_min = (now - memo.created_at).total_seconds() / 60.0
            if age_min >= self.settings.approval.pending_ttl_minutes / 2:
                self.emit(AlertLevel.INFO, "主動檢視:備忘錄等待審核", f"{memo.memo_id} 已等待 {age_min:.0f} 分鐘", memo.symbol, report)

    # ------------------------------------------------------------------
    def run_cycle(self) -> CycleReport:
        self.cycle += 1
        now = self.feed.now()
        if self.started_at is None:
            self.started_at = now
        report = CycleReport(cycle=self.cycle, time=now)

        # 1 自選清單更新
        symbols = self.refresh_watchlist()
        # 2 掃描
        results = self.scanner.scan_all(symbols)
        report.scanned = len(results)
        report.opportunities = sum(1 for r in results if r.snapshot.opportunity)
        prices = {r.snapshot.symbol: r.snapshot.price for r in results}
        for sym, err in self.scanner.errors.items():
            self.emit(AlertLevel.WARNING, "掃描失敗", err, sym, report)

        # 3 監控既有部位與備忘錄(用最新價)
        self._monitor_positions(prices, now, report)
        self._monitor_memos(prices, now, report)
        # 4 執行已核准
        self._execute_approved(prices, now, report)

        # 5 訊號 → 風險 → 研究 → 備忘錄
        busy = self.gate.active_symbols()
        if not self.settings.risk.allow_same_symbol:
            busy |= {p.symbol for p in self.broker.open_positions()}
        for res in results:
            if not res.snapshot.opportunity or res.snapshot.symbol in busy:
                continue
            memo = self._process_opportunity(res, now, report)
            if memo is not None:
                busy.add(res.snapshot.symbol)
        # 自動核准模式:同一輪就執行
        self._execute_approved(prices, now, report)

        # 6 狀態監控 / 7 主動檢視
        self._status(now, report, announce=(self.cycle % self.settings.monitor.status_every == 0))
        if self.cycle % self.settings.monitor.proactive_review_every == 0:
            self._proactive_review(prices, now, report)

        self.state.journal({"type": "cycle", **report.model_dump(mode="json", exclude={"alerts"})})
        self.last_report = report
        if self.replay:
            self.feed.advance()
        return report

    def run(self, cycles: int | None = None, interval: float | None = None, sleep: Callable[[float], None] = time.sleep) -> list[CycleReport]:
        interval = self.settings.monitor.interval_seconds if interval is None else interval
        reports: list[CycleReport] = []
        self.emit(AlertLevel.INFO, "系統全天候上線", f"監控循環啟用:{', '.join(self.watchlist)} | 來源 {self.settings.data_source} | 券商 {self.broker.name} | 研究 {getattr(self.analyst, 'name', '?')} | 核准 {self.settings.approval.mode}")
        while cycles is None or len(reports) < cycles:
            try:
                reports.append(self.run_cycle())
            except KeyboardInterrupt:
                self.emit(AlertLevel.WARNING, "人工中止", "監控循環停止")
                break
            except Exception as exc:  # 單輪失敗不能讓全天候系統停機
                log.exception("監控循環第 %s 輪失敗", self.cycle)
                self.emit(AlertLevel.CRITICAL, "循環錯誤", f"第 {self.cycle} 輪:{exc}")
            if getattr(self.feed, "exhausted", False):
                self.emit(AlertLevel.INFO, "回放結束", "資料已用盡")
                break
            if interval > 0 and (cycles is None or len(reports) < cycles):
                sleep(interval)
        return reports

    # 便利方法:給 CLI 印備忘錄
    @staticmethod
    def render(memo: DecisionMemo) -> str:
        return render_memo(memo)
