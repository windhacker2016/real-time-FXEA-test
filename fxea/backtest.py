"""回測:CSV 回放 + 紙上交易 + 自動核准 + 規則式研究 → 交易明細、權益曲線、統計。

回測衡量的是**機械規則本身**(掃描 → 訊號 → 計畫 → 風險 → 斷路器),
不含 Claude 研究層與人工篩選;結果是這套流程「底線」的表現,不是上限。

保真度:進出場計入點差、停損/停利用 K 棒高低點判斷(同棒觸及兩者先算停損)、
只在 K 棒收盤後產生訊號並於下一根起算。沒有滑價與隔夜利息。
"""
from __future__ import annotations

import csv
import json
import math
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app import build_loop
from .config import Settings, _deep_merge
from .data.csv_feed import CsvFeed
from .models import Position


@dataclass
class BacktestResult:
    trades: list[Position]
    equity: list[tuple[datetime, float]]
    stats: dict[str, Any]
    halts: list[dict[str, Any]] = field(default_factory=list)
    out_dir: Path | None = None


def run_backtest(
    settings: Settings,
    csv_dir: str | Path,
    symbols: list[str],
    start: datetime | None,
    out_dir: str | Path,
    cooldown_hours: int | None = 72,
    warmup: int = 300,
) -> BacktestResult:
    out = Path(out_dir)
    state_dir = out / "state"
    if state_dir.exists():
        shutil.rmtree(state_dir)
    out.mkdir(parents=True, exist_ok=True)

    overrides: dict[str, Any] = {
        "data_source": "csv",
        "csv_dir": str(csv_dir),
        "broker": "paper",
        "watchlist": [s.upper() for s in symbols],
        "state_dir": str(state_dir),
        "approval": {"mode": "auto"},
        "research": {"provider": "rules"},
        "alerts": {"console": False, "file": None, "webhook_url": None},
    }
    if cooldown_hours is not None:
        overrides["risk"] = {"drawdown_cooldown_hours": cooldown_hours}
    bt_settings = Settings.model_validate(_deep_merge(settings.model_dump(), overrides))

    feed = CsvFeed(csv_dir, bt_settings.watchlist, warmup=warmup, warmup_until=start)
    loop = build_loop(bt_settings, feed=feed)
    loop.run(cycles=None, interval=0)

    trades = [p for p in loop.broker.all_positions() if p.status == "closed" and (start is None or p.opened_at >= start)]  # type: ignore[attr-defined]

    equity: list[tuple[datetime, float]] = []
    halts: list[dict[str, Any]] = []
    journal = state_dir / "journal.jsonl"
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev.get("type") == "cycle":
                t = datetime.fromisoformat(ev["time"])
                if start is not None and t < start:
                    continue
                equity.append((t, float(ev["equity"])))
            elif ev.get("type") == "risk_state" and ev.get("to") == "halted":
                halts.append(ev)

    stats = compute_stats(trades, equity, halts)
    _write_csvs(out, trades, equity)
    return BacktestResult(trades=trades, equity=equity, stats=stats, halts=halts, out_dir=out)


# ---------------------------------------------------------------------------


def compute_stats(trades: list[Position], equity: list[tuple[datetime, float]], halts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    n = len(trades)
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n if n else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else (math.inf if avg_win > 0 else 0.0)
    breakeven = (1.0 / (1.0 + payoff)) if payoff not in (0.0, math.inf) else None
    expectancy = sum(pnls) / n if n else 0.0
    gross_loss = abs(sum(losses))
    profit_factor = (sum(wins) / gross_loss) if gross_loss > 0 else (math.inf if wins else 0.0)
    stderr = math.sqrt(win_rate * (1 - win_rate) / n) if n else 0.0

    start_eq = equity[0][1] if equity else None
    end_eq = equity[-1][1] if equity else None
    net = sum(pnls)
    return_pct = (net / start_eq * 100.0) if start_eq else 0.0

    peak, peak_t = -math.inf, None
    max_dd, dd_peak_t, dd_trough_t = 0.0, None, None
    for t, eq in equity:
        if eq > peak:
            peak, peak_t = eq, t
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd, dd_peak_t, dd_trough_t = dd, peak_t, t

    opens = sorted(t.opened_at for t in trades)
    gaps = [(b - a).total_seconds() / 86400.0 for a, b in zip(opens, opens[1:])]
    longest_gap = max(gaps) if gaps else 0.0
    tail_gap = ((equity[-1][0] - opens[-1]).total_seconds() / 86400.0) if (opens and equity) else 0.0
    span_days = ((equity[-1][0] - equity[0][0]).total_seconds() / 86400.0) if len(equity) > 1 else 0.0
    per_month = n / (span_days / 30.44) if span_days > 0 else 0.0

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "win_rate_stderr": stderr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "breakeven_win_rate": breakeven,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "net_pnl": net,
        "start_equity": start_eq,
        "end_equity": end_eq,
        "return_pct": return_pct,
        "max_drawdown_pct": max_dd,
        "max_dd_peak_time": dd_peak_t,
        "max_dd_trough_time": dd_trough_t,
        "stop_outs": sum(1 for t in trades if t.close_reason == "停損"),
        "take_profits": sum(1 for t in trades if t.close_reason == "停利"),
        "longest_gap_days": longest_gap,
        "tail_gap_days": tail_gap,
        "span_days": span_days,
        "trades_per_month": per_month,
        "halts": len(halts or []),
        "by_symbol": _by_symbol(trades),
    }


def _by_symbol(trades: list[Position]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for t in trades:
        d = out.setdefault(t.symbol, {"trades": 0, "wins": 0, "pnl": 0.0})
        d["trades"] += 1
        d["wins"] += 1 if t.pnl > 0 else 0
        d["pnl"] += t.pnl
    return out


def _write_csvs(out: Path, trades: list[Position], equity: list[tuple[datetime, float]]) -> None:
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["position_id", "memo_id", "symbol", "direction", "lots", "opened_at", "entry", "closed_at", "exit", "pnl", "reason"])
        for t in trades:
            w.writerow([t.position_id, t.memo_id, t.symbol, t.direction.value, t.lots, t.opened_at.isoformat(), t.entry_price, t.closed_at.isoformat() if t.closed_at else "", t.close_price, t.pnl, t.close_reason])
    with (out / "equity.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "equity"])
        for t, eq in equity:
            w.writerow([t.isoformat(), eq])


def _fmt_t(t: datetime | None) -> str:
    return t.strftime("%Y-%m-%d") if t else "-"


def format_report(result: BacktestResult) -> str:
    s = result.stats
    L = ["═══ 回測結果(機械規則,不含 Claude 研究層與人工篩選)═══"]
    if result.equity:
        L.append(f"期間:{_fmt_t(result.equity[0][0])} → {_fmt_t(result.equity[-1][0])}({s['span_days']:.0f} 天)")
    L.append(f"交易筆數:{s['trades']}(約 {s['trades_per_month']:.1f} 筆/月)  停損 {s['stop_outs']} / 停利 {s['take_profits']}")
    if s["trades"]:
        be = f"{s['breakeven_win_rate'] * 100:.1f}%" if s["breakeven_win_rate"] is not None else "-"
        L.append(f"勝率:{s['win_rate'] * 100:.1f}%(±{s['win_rate_stderr'] * 100:.1f}% 標準誤)  損益兩平勝率:{be}")
        L.append(f"平均獲利:{s['avg_win']:+.2f}  平均虧損:{s['avg_loss']:+.2f}  賠率:{s['payoff']:.2f}")
        L.append(f"每筆期望值:{s['expectancy']:+.2f}  獲利因子:{s['profit_factor']:.2f}")
    L.append(f"淨損益:{s['net_pnl']:+.2f}(起始 {s['start_equity'] or 0:.2f} → 結束 {s['end_equity'] or 0:.2f},{s['return_pct']:+.2f}%)")
    L.append(f"最大回撤:{s['max_drawdown_pct']:.2f}%(高點 {_fmt_t(s['max_dd_peak_time'])} → 低點 {_fmt_t(s['max_dd_trough_time'])})")
    L.append(f"回撤斷路器觸發:{s['halts']} 次  最長無交易間隔:{s['longest_gap_days']:.0f} 天  最後一筆至期末:{s['tail_gap_days']:.0f} 天")
    for sym, d in s["by_symbol"].items():
        L.append(f"  {sym}:{int(d['trades'])} 筆,勝 {int(d['wins'])},損益 {d['pnl']:+.2f}")
    for h in result.halts:
        L.append(f"  ⏸ {h.get('time', '')[:16]} {h.get('reason', '')}")
    if s["trades"] and s["breakeven_win_rate"] is not None:
        edge = (s["win_rate"] - s["breakeven_win_rate"]) * 100
        z = edge / (s["win_rate_stderr"] * 100) if s["win_rate_stderr"] > 0 else 0.0
        verdict = "統計上無法與零優勢區分" if abs(z) < 2 else ("優勢顯著" if z > 0 else "劣勢顯著")
        L.append(f"優勢:勝率高於損益兩平 {edge:+.1f} 個百分點(z ≈ {z:.1f},{verdict};樣本 {s['trades']} 筆)")
    if result.out_dir:
        L.append(f"明細:{result.out_dir / 'trades.csv'}  權益曲線:{result.out_dir / 'equity.csv'}")
    return "\n".join(L)


def parse_date(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
