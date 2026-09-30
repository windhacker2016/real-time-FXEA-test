"""圖 6 全天候監控循環:掃描 → 訊號 → 風險 → 監控 → 警示 → (回到掃描)。

外圈四個節點:自選清單更新、觸發警示、狀態監控、主動檢視。
「系統全天候上線 — 不會停機,沒有開閉市間隔」「監控循環啟用 — 持續監看,隨時待命」

回撤斷路器(:class:`fxea.risk.RiskGovernor`)觸發時,循環不會停:仍然監控部位、
備忘錄與狀態,只是不開新倉,並且用 CRITICAL 警示與每日提醒告訴你系統正在暫停。
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
from .models import (
    Account,
    Alert,
    AlertLevel,
    Candle,
    CycleReport,
    Decision,
    DecisionMemo,
    MemoStatus,
    RiskMode,
    Timeframe,
)
from .planner import TradePlanner, apply_research_adjustments
from .research import Analyst, ResearchContext, RuleBasedAnalyst
from .risk import RiskGovernor, RiskModule, RiskTransition
from .scanner import MarketScanner, ScanResult
from .signals import SignalEngine
from .state import StateStore

log = logging.getLogger("fxea.monitor")

_LIVE_SOURCES = {"mt5", "ig"}
_SYSTEM_LABEL = {RiskMode.NORMAL: "online", RiskMode.HALTED: "halted", RiskMode.RECOVERY: "recovery"}


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
        self.rules_analyst = RuleBasedAnalyst()  # 被風險阻擋的設定不花 AI 呼叫
        self.alerts = alerts
        self.state = state
        self.scanner = MarketScanner(feed, settings.scanner, news)
        self.engine = SignalEngine(settings.signals)
        self.planner = TradePlanner(settings.planner)
        self.risk = RiskModule(settings.risk)
        self.governor = RiskGovernor(settings.risk, state)
        self.gate = ApprovalGate(state, settings.approval)
        self.memos = MemoBuilder(state)
        self.base_watchlist = [s.upper() for s in settings.watchlist]
        self.watchlist: list[str] = list(self.base_watchlist)
        self.replay = settings.data_source not in _LIVE_SOURCES
        self.cycle = 0
        self.started_at: datetime | None = None
        self.last_report: CycleReport | None = None
        self._recent_blocks: dict[tuple[str, str], datetime] = {}
        self._last_halt_reminder: datetime | None = None
        self._last_memo_at: dict[str, datetime] = {}

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

    # ---- 監控:既有部位 / 回撤斷路器 / 待審備忘錄 ------------------------------
    def _monitor_positions(self, prices: dict[str, float], bars: dict[str, Candle], now: datetime, report: CycleReport) -> None:
        for pos in self.broker.mark_to_market(prices, now, bars):
            report.closed.append(pos.position_id)
            level = AlertLevel.WARNING if pos.pnl < 0 else AlertLevel.INFO
            self.emit(level, f"部位平倉:{pos.close_reason}", f"{pos.position_id} {pos.direction.label_zh} {pos.lots} 手 @ {pos.close_price} 損益 {pos.pnl:+.2f}", pos.symbol, report)
            if pos.memo_id:
                try:
                    memo = self.state.load_memo(pos.memo_id)
                    memo.note = f"部位 {pos.position_id} 已平倉:{pos.close_reason},損益 {pos.pnl:+.2f}"
                    memo.outcome_pnl = pos.pnl
                    memo.outcome_reason = pos.close_reason
                    memo.closed_at = pos.closed_at or now
                    self.state.save_memo(memo)
                except KeyError:
                    pass

    def _announce_transition(self, t: RiskTransition, report: CycleReport) -> None:
        cfg = self.settings.risk
        if t.to_mode is RiskMode.HALTED:
            hint = f" 冷卻 {cfg.drawdown_cooldown_hours} 小時後自動以縮減風險恢復。" if cfg.drawdown_cooldown_hours > 0 else " 系統不會自行恢復:確認狀況後執行 fxea resume。"
            self.emit(AlertLevel.CRITICAL, "交易暫停:觸及回撤上限", t.reason + hint, None, report)
        elif t.to_mode is RiskMode.RECOVERY:
            self.emit(AlertLevel.WARNING, "恢復交易(縮減風險)", t.reason, None, report)
        else:
            self.emit(AlertLevel.INFO, "風險模式恢復正常", t.reason, None, report)
        self.state.journal({"type": "risk_state", "time": t.time.isoformat(), "from": t.from_mode.value, "to": t.to_mode.value, "reason": t.reason, "by": t.by})
        if t.to_mode is RiskMode.HALTED:
            self._last_halt_reminder = t.time

    def _govern(self, now: datetime, report: CycleReport) -> Account:
        account = self.broker.account()
        for t in self.governor.update(account, now):
            self._announce_transition(t, report)
        st = self.governor.state
        if st.mode is RiskMode.HALTED and (self._last_halt_reminder is None or now - self._last_halt_reminder >= timedelta(hours=24)):
            self._last_halt_reminder = now
            since = f"{st.halted_at:%Y-%m-%d %H:%M}" if st.halted_at else "?"
            cfg = self.settings.risk
            how = f"冷卻 {cfg.drawdown_cooldown_hours} 小時後自動恢復" if cfg.drawdown_cooldown_hours > 0 else "執行 fxea resume 恢復"
            self.emit(AlertLevel.WARNING, "交易暫停中(回撤斷路器)", f"自 {since} 起未開新倉。{st.halt_reason};{how}", None, report)
        return account

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
                zone = f"{memo.plan.entry_zone_low}–{memo.plan.entry_zone_high}"
                if self.settings.approval.mode == "auto":
                    self.gate.decide(memo.memo_id, Decision.APPROVE, by="auto:zone", now=now)
                    self.emit(AlertLevel.WARNING, "觀察清單條件達成,自動核准", f"{memo.memo_id} 價格 {price} 進入進場區 {zone}", memo.symbol, report)
                else:
                    self.emit(AlertLevel.WARNING, "觀察清單條件達成", f"{memo.memo_id} 價格 {price} 進入進場區 {zone},可執行 fxea decide {memo.memo_id} approve", memo.symbol, report)

    # ---- 執行已核准 --------------------------------------------------------
    def _execute_approved(self, prices: dict[str, float], now: datetime, report: CycleReport) -> None:
        for memo in self.gate.approved_unexecuted():
            if self.governor.halted:
                self.gate.expire(memo, "交易暫停中(回撤斷路器),未執行", now)
                self.emit(AlertLevel.WARNING, "交易暫停中,已核准備忘錄未執行", f"{memo.memo_id};恢復交易後需重新產生設定", memo.symbol, report)
                continue
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
    def _recent_trades(self, symbol: str, n: int) -> list[dict]:
        """同商品近期已平倉交易(給 AI 看它自己的戰績)。"""
        if n <= 0:
            return []
        done = [m for m in self.state.list_memos() if m.symbol == symbol and m.outcome_pnl is not None]
        done.sort(key=lambda m: m.closed_at or m.created_at, reverse=True)
        return [
            {
                "memo_id": m.memo_id,
                "opened": m.created_at.isoformat(),
                "closed": m.closed_at.isoformat() if m.closed_at else None,
                "signal": m.primary_signal.signal_type.value,
                "timeframe": m.primary_signal.timeframe.value,
                "direction": m.plan.direction.value,
                "score": m.primary_signal.score,
                "research": m.research.recommendation,
                "pnl": m.outcome_pnl,
                "r_multiple": m.r_multiple,
                "reason": m.outcome_reason,
            }
            for m in done[:n]
        ]

    def _process_opportunity(self, res: ScanResult, now: datetime, report: CycleReport) -> DecisionMemo | None:
        snapshot = res.snapshot
        signals = self.engine.detect(snapshot, res.candles)
        if not signals:
            return None
        report.signals += len(signals)
        plan = self.planner.build(signals[0], snapshot, res.candles)
        account = self.broker.account()
        open_positions = self.broker.open_positions()
        base_scale = self.governor.risk_scale
        dd = self.governor.drawdown_pct(account.equity)
        risk = self.risk.evaluate(plan, snapshot, account, open_positions, now, risk_scale=base_scale, drawdown_pct=dd, mode=self.governor.mode)
        eligible = risk.passed and plan.valid

        # 同商品、同樣的阻擋原因在冷卻期內只記錄一次(避免洗版,也省下研究層呼叫)
        if not eligible:
            failed = ",".join(c.name for c in risk.checks if not c.passed) or "plan_invalid"
            key = (snapshot.symbol, failed)
            last = self._recent_blocks.get(key)
            cooldown = timedelta(minutes=self.settings.monitor.block_cooldown_minutes)
            if last is not None and now - last < cooldown:
                return None
            self._recent_blocks[key] = now

        # 研究層:通過風險檢查的設定交給 AI;被阻擋的用規則式(省錢)
        rcfg = self.settings.research
        analyst = self.analyst if (eligible or not rcfg.only_when_ready) else self.rules_analyst
        ctx = ResearchContext(
            snapshot,
            signals,
            plan,
            risk,
            open_positions,
            candles=res.candles,
            recent_trades=self._recent_trades(snapshot.symbol, rcfg.recent_trades),
            now=now,
        )
        research = analyst.assess(ctx)

        # AI 交易員層:界限內調整計畫、信念部位 → 風險模組重新驗算
        if eligible:
            adjusted, applied, ignored = apply_research_adjustments(plan, research, self.settings.planner)
            research.applied_adjustments = applied
            research.ignored_suggestions = ignored
            fraction = research.risk_fraction
            if applied or fraction != 1.0:
                plan = adjusted
                risk = self.risk.evaluate(plan, snapshot, account, open_positions, now, risk_scale=base_scale * fraction, drawdown_pct=dd, mode=self.governor.mode)

        memo = self.memos.build(snapshot, signals, plan, risk, research, now)
        memo = self.gate.submit(memo, now)
        self._last_memo_at[snapshot.symbol] = now

        sig = signals[0]
        if memo.status is MemoStatus.READY:
            report.memos_created.append(memo.memo_id)
            how = {
                Decision.APPROVE: "自動核准,將執行",
                Decision.WATCHLIST: "研究建議觀察 → 觀察清單",
                Decision.REJECT: "研究建議略過 → 已拒絕",
                Decision.PENDING: "需要人工核准",
            }[memo.decision]
            extras = ""
            if research.applied_adjustments:
                extras += " AI 調整:" + ";".join(research.applied_adjustments)
            if research.risk_fraction != 1.0:
                extras += f" 信念部位 ×{research.risk_fraction:g}"
            self.emit(
                AlertLevel.WARNING,
                f"交易計畫完成 — {how}",
                f"{memo.memo_id} {sig.signal_type.label_zh} {plan.direction.label_zh} 評分 {sig.score:.0f} "
                f"進場 {plan.entry_price} 停損 {plan.stop_loss} 停利 {plan.take_profit} R:R 1:{plan.risk_reward:.2f} "
                f"信心 {research.stars} 研究建議:{research.recommendation_zh}{extras}",
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
        st = self.governor.state
        dd = self.governor.drawdown_pct(account.equity)
        report.equity = account.equity
        report.drawdown_pct = round(dd, 3)
        report.risk_mode = st.mode
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
            "reference_peak": st.reference_peak,
            "all_time_peak": account.peak_equity,
            "daily_pnl": account.daily_pnl,
            "risk_mode": st.mode.value,
            "halt_reason": st.halt_reason if st.mode is RiskMode.HALTED else None,
            "halted_at": st.halted_at.isoformat() if st.halted_at else None,
            "halts": st.halts,
            "open_positions": [p.model_dump(mode="json") for p in open_positions],
            "pending_memos": [m.memo_id for m in self.gate.pending()],
            "scanner_errors": self.scanner.errors,
            "system": _SYSTEM_LABEL[st.mode],
        }
        self.state.save_status(status)
        if announce:
            self.emit(
                AlertLevel.INFO,
                "狀態監控",
                f"第 {self.cycle} 輪 | 權益 {account.equity:.2f} | 回撤 {dd:.2f}% | 今日 {account.daily_pnl:+.2f} | 未平倉 {len(open_positions)} | 待審 {report.pending_memos} | 風險模式 {st.mode.label_zh}",
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
    @staticmethod
    def _latest_bars(results: list[ScanResult]) -> dict[str, Candle]:
        bars: dict[str, Candle] = {}
        for r in results:
            df = r.candles.get(Timeframe.H1)
            if df is None or df.empty:
                continue
            row = df.iloc[-1]
            t = row["time"]
            t = t.to_pydatetime() if hasattr(t, "to_pydatetime") else t
            bars[r.snapshot.symbol] = Candle(time=t, open=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]))
        return bars

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
        bars = self._latest_bars(results)
        for sym, err in self.scanner.errors.items():
            self.emit(AlertLevel.WARNING, "掃描失敗", err, sym, report)

        # 3 監控既有部位(用最新 K 棒高低點)→ 回撤斷路器 → 備忘錄
        self._monitor_positions(prices, bars, now, report)
        self._govern(now, report)
        self._monitor_memos(prices, now, report)
        # 4 執行已核准(暫停中會拒絕並失效)
        self._execute_approved(prices, now, report)

        # 5 訊號 → 風險 → 研究 → 備忘錄(暫停中不開新設定)
        if not self.governor.halted:
            busy = self.gate.active_symbols()
            if not self.settings.risk.allow_same_symbol:
                busy |= {p.symbol for p in self.broker.open_positions()}
            window = timedelta(minutes=self.settings.monitor.min_minutes_between_memos)
            for res in results:
                sym = res.snapshot.symbol
                if not res.snapshot.opportunity or sym in busy:
                    continue
                last = self._last_memo_at.get(sym)
                if last is not None and now - last < window:
                    continue
                memo = self._process_opportunity(res, now, report)
                if memo is not None:
                    busy.add(sym)
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

    def run(
        self,
        cycles: int | None = None,
        interval: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        fail_fast: bool = False,
    ) -> list[CycleReport]:
        """``fail_fast=True``(回測用):任何一輪出錯直接拋出,而不是記錄後繼續。"""
        interval = self.settings.monitor.interval_seconds if interval is None else interval
        reports: list[CycleReport] = []
        mode = self.governor.mode
        self.emit(
            AlertLevel.INFO,
            "系統全天候上線",
            f"監控循環啟用:{', '.join(self.watchlist)} | 來源 {self.settings.data_source} | 券商 {self.broker.name} | 研究 {getattr(self.analyst, 'name', '?')} | 核准 {self.settings.approval.mode} | 風險模式 {mode.label_zh}",
        )
        attempts = 0  # 以「嘗試次數」計數:失敗的一輪也算,否則持續出錯會無限迴圈
        while cycles is None or attempts < cycles:
            attempts += 1
            try:
                reports.append(self.run_cycle())
            except KeyboardInterrupt:
                self.emit(AlertLevel.WARNING, "人工中止", "監控循環停止")
                break
            except Exception as exc:  # 單輪失敗不能讓全天候系統停機
                if fail_fast:
                    raise
                log.exception("監控循環第 %s 輪失敗", self.cycle)
                self.emit(AlertLevel.CRITICAL, "循環錯誤", f"第 {self.cycle} 輪:{exc}")
                if self.replay:
                    self.feed.advance()  # 回放來源仍要前進,否則同一根 K 棒一直重跑
            if getattr(self.feed, "exhausted", False):
                self.emit(AlertLevel.INFO, "回放結束", "資料已用盡")
                break
            if interval > 0 and (cycles is None or attempts < cycles):
                sleep(interval)
        return reports

    # 便利方法:給 CLI 印備忘錄
    @staticmethod
    def render(memo: DecisionMemo) -> str:
        return render_memo(memo)
