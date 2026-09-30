from fxea.cli import main
from fxea.state import StateStore


def test_cli_run_status_pending_decide(tmp_path, capsys):
    state = str(tmp_path / "state")
    assert main(["--state-dir", state, "run", "--cycles", "40", "--interval", "0"]) == 0
    out = capsys.readouterr().out
    assert "系統全天候上線" in out and "完成 40 輪" in out

    assert main(["--state-dir", state, "status"]) == 0
    out = capsys.readouterr().out
    assert "第 40 輪" in out and "待審備忘錄" in out

    assert main(["--state-dir", state, "pending"]) == 0
    out = capsys.readouterr().out
    memos = StateStore(state).list_memos()
    pending = [m for m in memos if m.awaiting_human]
    if pending:
        assert pending[0].memo_id in out
        assert main(["--state-dir", state, "decide", pending[0].memo_id, "watchlist", "--by", "cli"]) == 0
        assert "觀察清單" in capsys.readouterr().out
        assert main(["--state-dir", state, "memo", pending[0].memo_id]) == 0
    else:
        assert "沒有等待審核" in out

    assert main(["--state-dir", state, "decide", "FD-999-0101", "approve"]) == 1
    assert main(["--state-dir", state, "memo", "FD-999-0101"]) == 1


def test_cli_scan(tmp_path, capsys):
    assert main(["--state-dir", str(tmp_path / "s"), "scan"]) == 0
    out = capsys.readouterr().out
    assert "EURUSD" in out
