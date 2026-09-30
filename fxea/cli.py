"""命令列:

  fxea run [--config cfg.yaml] [--cycles N] [--interval SEC]   啟動全天候監控循環
  fxea scan [--config ...]                                    掃描一次並列出快照/訊號
  fxea pending                                                列出等待人工審核的備忘錄
  fxea decide <memo_id> approve|watchlist|reject [--note ..]  人工決策
  fxea memo <memo_id>                                         顯示備忘錄
  fxea status                                                 帳戶、部位、待審、風險模式摘要
  fxea resume [--full-risk]                                   回撤斷路器暫停後,人工恢復交易
  fxea backtest --csv-dir DIR [--from YYYY-MM-DD] ...         用 CSV 回放做回測
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .app import build_loop
from .approval import ApprovalGate
from .config import Settings, _deep_merge, load_settings, parse_set_args
from .memo import render_memo
from .models import Decision
from .risk import RiskGovernor
from .state import StateStore


def _settings(args: argparse.Namespace) -> Settings:
    path = args.config
    if path is None and Path("config/default.yaml").exists():
        path = "config/default.yaml"
    overrides: dict = {}
    if getattr(args, "state_dir", None):
        overrides["state_dir"] = args.state_dir
    if getattr(args, "auto", False):
        overrides["approval"] = {"mode": "auto"}
    if getattr(args, "honor_research", False):
        overrides["approval"] = {**overrides.get("approval", {}), "honor_research": True}
    if getattr(args, "research", None):
        overrides["research"] = {"provider": args.research}
    overrides = _deep_merge(overrides, parse_set_args(getattr(args, "set", None)))
    return load_settings(path, overrides)


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    loop = build_loop(settings)
    reports = loop.run(cycles=args.cycles, interval=args.interval)
    last = reports[-1] if reports else None
    if last:
        print(f"\n完成 {len(reports)} 輪;權益 {last.equity:.2f},回撤 {last.drawdown_pct:.2f}%,未平倉 {last.open_positions},待審 {last.pending_memos},風險模式 {last.risk_mode.label_zh}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    settings = _settings(args)
    loop = build_loop(settings)
    for res in loop.scanner.scan_all(loop.refresh_watchlist()):
        s = res.snapshot
        flag = "★ 機會" if s.opportunity else "  -"
        print(f"{flag} {s.symbol} {s.price:.5g} Δ{s.change_pct:+.2f}% 量×{s.volume_ratio:.2f} H1 {s.trend_h1.label_zh} H4 {s.trend_h4.label_zh} ATR% {s.atr_percentile:.0f} RSI {s.rsi_h1:.0f}")
        for note in s.notes:
            print(f"      · {note}")
        for sig in loop.engine.detect(s, res.candles):
            print(f"      ▶ 訊號 {sig.signal_type.label_zh} {sig.direction.label_zh} {sig.timeframe.value} 評分 {sig.score:.0f}:{'; '.join(sig.reasons)}")
    for sym, err in loop.scanner.errors.items():
        print(f"  ! {sym} 掃描失敗:{err}")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    settings = _settings(args)
    gate = ApprovalGate(StateStore(settings.state_dir), settings.approval)
    memos = gate.pending()
    if not memos:
        print("沒有等待審核的備忘錄。")
        return 0
    for m in memos:
        print(render_memo(m))
        print()
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = StateStore(settings.state_dir)
    gate = ApprovalGate(store, settings.approval)
    governor = RiskGovernor(settings.risk, store)
    if governor.halted and args.decision == "approve":
        print(f"警告:風險模式為「暫停」({governor.state.halt_reason}),核准的備忘錄不會被執行;請先 fxea resume。", file=sys.stderr)
    try:
        memo = gate.decide(args.memo_id, Decision(args.decision), by=args.by, note=args.note)
    except (KeyError, ValueError) as exc:
        print(f"錯誤:{exc}", file=sys.stderr)
        return 1
    print(render_memo(memo))
    return 0


def cmd_memo(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = StateStore(settings.state_dir)
    try:
        print(render_memo(store.load_memo(args.memo_id)))
    except KeyError as exc:
        print(f"錯誤:{exc}", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = StateStore(settings.state_dir)
    status = store.load_status()
    if not status:
        print("尚無狀態(先執行 fxea run)。")
        return 0
    print(f"時間 {status.get('time')}  第 {status.get('cycle')} 輪  運作 {status.get('uptime_seconds', 0) // 60} 分鐘  系統 {status.get('system')}")
    print(f"權益 {status.get('equity')}  餘額 {status.get('balance')}  回撤 {status.get('drawdown_pct')}%(高水位 {status.get('reference_peak')})  今日 {status.get('daily_pnl')}")
    mode = status.get("risk_mode", "normal")
    line = f"風險模式 {mode}  斷路器觸發 {status.get('halts', 0)} 次"
    if mode == "halted":
        line += f"\n  ⏸ 交易暫停中,自 {status.get('halted_at')}:{status.get('halt_reason')}\n  → 確認狀況後執行 fxea resume(或 fxea resume --full-risk)"
    print(line)
    print(f"自選清單 {', '.join(status.get('watchlist', []))}")
    for p in status.get("open_positions", []):
        print(f"  部位 {p['position_id']} {p['symbol']} {p['direction']} {p['lots']} 手 @ {p['entry_price']} SL {p['stop_loss']} TP {p['take_profit']}")
    pend = status.get("pending_memos", [])
    print(f"待審備忘錄 {len(pend)}:{', '.join(pend) if pend else '-'}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = StateStore(settings.state_dir)
    governor = RiskGovernor(settings.risk, store)
    try:
        t = governor.resume(datetime.now(timezone.utc), by=args.by, full_risk=args.full_risk)
    except ValueError as exc:
        print(f"錯誤:{exc}", file=sys.stderr)
        return 1
    store.journal({"type": "risk_state", "time": t.time.isoformat(), "from": t.from_mode.value, "to": t.to_mode.value, "reason": t.reason, "by": t.by})
    print(f"風險模式:{t.from_mode.label_zh} → {t.to_mode.label_zh}\n{t.reason}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest import format_comparison, format_report, parse_date, run_backtest, run_by_type

    settings = _settings(args)
    symbols = args.symbol or settings.watchlist
    start = parse_date(args.start) if args.start else None
    out = args.out or f"backtests/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    overrides: dict = {}
    if args.research == "claude":
        # 用 AI 層回測:採納研究建議,且不允許悄悄退回規則式
        overrides["research"] = {"provider": "claude"}
        overrides["approval"] = {"honor_research": True}
    try:
        common = dict(cooldown_hours=args.cooldown_hours, warmup=args.warmup, overrides=overrides)
        if args.by_type:
            results = run_by_type(settings, args.csv_dir, symbols, start, out, **common)
            for r in results:
                print(format_report(r))
                print()
            print(format_comparison(results))
        else:
            print(format_report(run_backtest(settings, args.csv_dir, symbols, start, out, **common)))
    except RuntimeError as exc:
        print(f"回測中止:{exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fxea", description="全天候 AI 外匯交易員")
    p.add_argument("--config", help="YAML 設定檔(預設 config/default.yaml)")
    p.add_argument("--state-dir", dest="state_dir", help="覆寫狀態目錄")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="覆寫任一設定,例如 --set signals.min_score=70 --set scanner.trading_hours_utc=[7,17]")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="啟動監控循環")
    r.add_argument("--cycles", type=int, default=None, help="執行輪數(預設無限)")
    r.add_argument("--interval", type=float, default=None, help="每輪間隔秒數(覆寫設定)")
    r.add_argument("--auto", action="store_true", help="自動核准(僅建議紙上交易)")
    r.add_argument("--honor-research", dest="honor_research", action="store_true", help="auto 模式採納研究建議(execute/watch/skip)")
    r.add_argument("--research", choices=["rules", "claude"], help="覆寫研究分析器")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("scan", help="掃描一次")
    s.set_defaults(func=cmd_scan)

    pe = sub.add_parser("pending", help="列出待審備忘錄")
    pe.set_defaults(func=cmd_pending)

    d = sub.add_parser("decide", help="人工決策")
    d.add_argument("memo_id")
    d.add_argument("decision", choices=["approve", "watchlist", "reject"])
    d.add_argument("--by", default="human")
    d.add_argument("--note")
    d.set_defaults(func=cmd_decide)

    m = sub.add_parser("memo", help="顯示備忘錄")
    m.add_argument("memo_id")
    m.set_defaults(func=cmd_memo)

    st = sub.add_parser("status", help="狀態摘要")
    st.set_defaults(func=cmd_status)

    rs = sub.add_parser("resume", help="回撤斷路器暫停後恢復交易")
    rs.add_argument("--by", default="human")
    rs.add_argument("--full-risk", action="store_true", help="直接回復全額風險(預設為恢復期、風險縮減)")
    rs.set_defaults(func=cmd_resume)

    bt = sub.add_parser("backtest", help="CSV 回放回測")
    bt.add_argument("--csv-dir", required=True, help="含 {SYMBOL}_H1.csv 的資料夾")
    bt.add_argument("--symbol", action="append", help="商品(可重複);預設用設定的自選清單")
    bt.add_argument("--from", dest="start", help="正式起算日 YYYY-MM-DD(之前的資料只當暖機)")
    bt.add_argument("--out", help="輸出資料夾(預設 backtests/<時間戳>)")
    bt.add_argument("--cooldown-hours", type=int, default=72, help="回撤斷路器冷卻小時數(預設 72;0 = 觸發後不再交易)")
    bt.add_argument("--warmup", type=int, default=300, help="指標暖機至少幾根 H1")
    bt.add_argument("--research", choices=["rules", "claude"], default="rules", help="claude:每個候選設定交給 Claude 判斷並採納(花 API 費用)")
    bt.add_argument("--honor-research", dest="honor_research", action="store_true", help="rules 模式也採納規則式研究建議")
    bt.add_argument("--by-type", dest="by_type", action="store_true", help="基準 + 五種訊號型態各跑一次,印比較表")
    bt.set_defaults(func=cmd_backtest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
