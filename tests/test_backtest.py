from datetime import timedelta

from fxea.backtest import compute_stats
from fxea.cli import main
from fxea.data.synthetic import SyntheticFeed
from fxea.models import Direction, Position, Timeframe

from .helpers import T0


def _pos(i: int, pnl: float, reason: str) -> Position:
    return Position(
        position_id=f"P-{i}",
        symbol="EURUSD",
        direction=Direction.LONG,
        lots=0.1,
        entry_price=1.0,
        stop_loss=0.99,
        take_profit=1.02,
        opened_at=T0 + timedelta(days=i),
        closed_at=T0 + timedelta(days=i, hours=5),
        status="closed",
        pnl=pnl,
        close_reason=reason,
    )


def test_compute_stats_basic():
    trades = [_pos(0, 200, "停利"), _pos(1, -100, "停損"), _pos(2, -100, "停損"), _pos(3, 220, "停利")]
    equity = [(T0 + timedelta(days=d), e) for d, e in enumerate([10_000.0, 10_200.0, 10_100.0, 10_000.0, 10_220.0])]
    s = compute_stats(trades, equity)
    assert s["trades"] == 4 and s["wins"] == 2 and s["win_rate"] == 0.5
    assert s["avg_win"] == 210 and s["avg_loss"] == -100 and abs(s["payoff"] - 2.1) < 1e-9
    assert abs(s["breakeven_win_rate"] - 1 / 3.1) < 1e-9
    assert s["expectancy"] == 55 and abs(s["profit_factor"] - 2.1) < 1e-9
    assert s["net_pnl"] == 220 and abs(s["return_pct"] - 2.2) < 1e-9
    assert abs(s["max_drawdown_pct"] - 200 / 10_200 * 100) < 1e-9
    assert s["stop_outs"] == 2 and s["take_profits"] == 2 and s["longest_gap_days"] == 1.0


def test_backtest_cli_end_to_end(tmp_path, capsys):
    feed = SyntheticFeed(["EURUSD"], seed=11, bars=1400)
    df = feed.candles("EURUSD", Timeframe.H1, 1400)
    data = tmp_path / "data"
    data.mkdir()
    df.to_csv(data / "EURUSD_H1.csv", index=False)
    start = df["time"].iloc[500].strftime("%Y-%m-%d")
    out = tmp_path / "bt"

    rc = main(["backtest", "--csv-dir", str(data), "--symbol", "EURUSD", "--from", start, "--out", str(out), "--cooldown-hours", "72"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "交易筆數" in text and "最大回撤" in text and "回撤斷路器" in text and "損益兩平勝率" in text
    trades = (out / "trades.csv").read_text().splitlines()
    assert len(trades) >= 2  # 標題 + 至少一筆
    equity = (out / "equity.csv").read_text().splitlines()
    assert equity[1].startswith(start)  # 權益曲線從正式起算日開始,之前只當暖機
    assert "研究層:" in text and "機械規則" in text


def test_backtest_by_type_and_set_overrides(tmp_path, capsys):
    feed = SyntheticFeed(["EURUSD"], seed=11, bars=650)
    df = feed.candles("EURUSD", Timeframe.H1, 650)
    data = tmp_path / "data"
    data.mkdir()
    df.to_csv(data / "EURUSD_H1.csv", index=False)
    start = df["time"].iloc[330].strftime("%Y-%m-%d")

    rc = main(["--set", "signals.min_score=55", "backtest", "--csv-dir", str(data), "--symbol", "EURUSD", "--from", start, "--out", str(tmp_path / "bt"), "--by-type"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "各訊號型態單獨回測" in text and "全部(基準)" in text
    for label in ("突破", "拉回", "動能", "趨勢延續", "反轉"):
        assert label in text
    assert (tmp_path / "bt" / "all" / "trades.csv").exists() and (tmp_path / "bt" / "breakout" / "trades.csv").exists()

    # --set 真的生效:把門檻拉到 100 就不會有任何交易
    rc = main(["--set", "signals.min_score=100", "backtest", "--csv-dir", str(data), "--from", start, "--out", str(tmp_path / "bt2")])
    assert rc == 0 and "交易筆數:0" in capsys.readouterr().out


def test_backtest_claude_api_failure_aborts_instead_of_silently_using_rules(tmp_path, capsys, monkeypatch):
    # 假金鑰 + 指到本機不可達的埠:連線立刻被拒 → strict 模式必須中止回測,而不是悄悄退回規則式
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    feed = SyntheticFeed(["EURUSD"], seed=11, bars=650)
    data = tmp_path / "data"
    data.mkdir()
    feed.candles("EURUSD", Timeframe.H1, 650).to_csv(data / "EURUSD_H1.csv", index=False)
    rc = main(
        ["--set", "research.max_retries=0", "--set", "research.timeout_seconds=2", "--set", "signals.min_score=55",
         "backtest", "--csv-dir", str(data), "--out", str(tmp_path / "bt"), "--research", "claude"]
    )
    assert rc == 1
    assert "回測中止" in capsys.readouterr().err
