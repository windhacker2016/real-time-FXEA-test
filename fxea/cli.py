"""命令列:

  fxea run [--config cfg.yaml] [--cycles N] [--interval SEC]   啟動全天候監控循環
  fxea scan [--config ...]                                    掃描一次並列出快照/訊號
  fxea pending                                                列出等待人工審核的備忘錄
  fxea decide <memo_id> approve|watchlist|reject [--note ..]  人工決策
  fxea memo <memo_id>                                         顯示備忘錄
  fxea status                                                 帳戶、部位、待審摘要
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .app import build_loop
from .approval import ApprovalGate
from .config import Settings, load_settings
from .memo import render_memo
from .models import Decision
from .state import StateStore


def _settings(args: argparse.Namespace) -> Settings:
    path = args.config
    if path is None and Path("config/default.yaml").exists():
        path = "config/default.yaml"
    overrides = {}
    if getattr(args, "state_dir", None):
        overrides["state_dir"] = args.state_dir
    if getattr(args, "auto", False):
        overrides["approval"] = {"mode": "auto"}
    if getattr(args, "research", None):
        overrides["research"] = {"provider": args.research}
    return load_settings(path, overrides)


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    loop = build_loop(settings)
    reports = loop.run(cycles=args.cycles, interval=args.interval)
    last = reports[-1] if reports else None
    if last:
        print(f"\n完成 {len(reports)} 輪;權益 {last.equity:.2f},回撤 {last.drawdown_pct:.2f}%,未平倉 {last.open_positions},待審 {last.pending_memos}")
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
    gate = ApprovalGate(StateStore(settings.state_dir), settings.approval)
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
    print(f"權益 {status.get('equity')}  餘額 {status.get('balance')}  回撤 {status.get('drawdown_pct')}%  今日 {status.get('daily_pnl')}")
    print(f"自選清單 {', '.join(status.get('watchlist', []))}")
    for p in status.get("open_positions", []):
        print(f"  部位 {p['position_id']} {p['symbol']} {p['direction']} {p['lots']} 手 @ {p['entry_price']} SL {p['stop_loss']} TP {p['take_profit']}")
    pend = status.get("pending_memos", [])
    print(f"待審備忘錄 {len(pend)}:{', '.join(pend) if pend else '-'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fxea", description="全天候 AI 外匯交易員")
    p.add_argument("--config", help="YAML 設定檔(預設 config/default.yaml)")
    p.add_argument("--state-dir", dest="state_dir", help="覆寫狀態目錄")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="啟動監控循環")
    r.add_argument("--cycles", type=int, default=None, help="執行輪數(預設無限)")
    r.add_argument("--interval", type=float, default=None, help="每輪間隔秒數(覆寫設定)")
    r.add_argument("--auto", action="store_true", help="自動核准(僅建議紙上交易)")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
