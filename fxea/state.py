"""持久化狀態:備忘錄、帳戶、部位、日誌、計數器(全部是 JSON 檔,方便人工檢視)。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Account, DecisionMemo, Position


class StateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.memo_dir = self.root / "memos"
        self.memo_dir.mkdir(parents=True, exist_ok=True)
        self.counter_file = self.root / "counters.json"
        self.account_file = self.root / "account.json"
        self.positions_file = self.root / "positions.json"
        self.journal_file = self.root / "journal.jsonl"
        self.status_file = self.root / "status.json"

    # ---- 計數器 --------------------------------------------------------
    def next_seq(self, key: str) -> int:
        data = self._read_json(self.counter_file) or {}
        data[key] = int(data.get(key, 0)) + 1
        self._write_json(self.counter_file, data)
        return data[key]

    # ---- 備忘錄 --------------------------------------------------------
    # 監控循環每輪會列舉備忘錄十幾次;以 (路徑, mtime) 快取避免重複解析幾百個 JSON,
    # 目錄本身最多每秒重掃一次(即時模式每輪 ≥ 1 秒,仍看得到 `fxea decide` 從別的程序寫入的變更)
    _RESCAN_SECONDS = 1.0

    def _cache(self) -> dict[str, tuple[float, DecisionMemo]]:
        return self.__dict__.setdefault("_memo_cache", {})

    def _cached(self, path: Path) -> DecisionMemo:
        cache = self._cache()
        mtime = path.stat().st_mtime
        hit = cache.get(path.name)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        memo = DecisionMemo.model_validate_json(path.read_text(encoding="utf-8"))
        cache[path.name] = (mtime, memo)
        return memo

    def save_memo(self, memo: DecisionMemo) -> None:
        path = self.memo_dir / f"{memo.memo_id}.json"
        path.write_text(memo.model_dump_json(indent=2), encoding="utf-8")
        self._cache()[path.name] = (path.stat().st_mtime, memo)

    def load_memo(self, memo_id: str) -> DecisionMemo:
        path = self.memo_dir / f"{memo_id}.json"
        if not path.exists():
            raise KeyError(f"找不到備忘錄 {memo_id}")
        return self._cached(path).model_copy(deep=True)

    def list_memos(self) -> list[DecisionMemo]:
        now = time.monotonic()
        stamp = self.__dict__.get("_memo_scan_stamp")
        if stamp is None or now - stamp > self._RESCAN_SECONDS:
            for p in self.memo_dir.glob("*.json"):
                self._cached(p)
            self.__dict__["_memo_scan_stamp"] = now
        return sorted((m for _, m in self._cache().values()), key=lambda m: m.created_at)

    # ---- 帳戶 / 部位 ---------------------------------------------------
    def save_account(self, account: Account) -> None:
        self.account_file.write_text(account.model_dump_json(indent=2), encoding="utf-8")

    def load_account(self) -> Account | None:
        if not self.account_file.exists():
            return None
        return Account.model_validate_json(self.account_file.read_text(encoding="utf-8"))

    def save_positions(self, positions: list[Position]) -> None:
        payload = [p.model_dump(mode="json") for p in positions]
        self._write_json(self.positions_file, payload)

    def load_positions(self) -> list[Position]:
        data = self._read_json(self.positions_file) or []
        return [Position.model_validate(item) for item in data]

    # ---- 日誌 / 狀態 ---------------------------------------------------
    def journal(self, event: dict[str, Any]) -> None:
        record = {"logged_at": datetime.now(timezone.utc).isoformat(), **event}
        with self.journal_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def save_status(self, status: dict[str, Any]) -> None:
        self._write_json(self.status_file, status)

    def load_status(self) -> dict[str, Any]:
        return self._read_json(self.status_file) or {}

    # ---- 通用 JSON -----------------------------------------------------
    def read_json(self, name: str) -> Any:
        return self._read_json(self.root / name)

    def write_json(self, name: str, data: Any) -> None:
        self._write_json(self.root / name, data)

    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
