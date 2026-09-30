"""從已登入的 MT5 終端匯出 H1 K 棒成 FXEA 回測用的 CSV(Windows + ``pip install MetaTrader5``)。

用法:
    python scripts/export_mt5_csv.py EURUSD 2025-11-15 data/
    python scripts/export_mt5_csv.py EURUSD 2025-11-15 data/ --to 2026-09-30 --broker-symbol EURUSD.a

輸出:data/EURUSD_H1.csv(time,open,high,low,close,volume;time 為 UTC)。
此腳本未在 Linux 開發環境實測(MetaTrader5 套件僅 Windows)。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="FXEA 使用的代碼,例如 EURUSD")
    ap.add_argument("start", help="起始日 YYYY-MM-DD(UTC)")
    ap.add_argument("out_dir")
    ap.add_argument("--to", dest="end", help="結束日 YYYY-MM-DD(預設現在)")
    ap.add_argument("--broker-symbol", help="券商端代碼若不同(例如 EURUSD.a)")
    args = ap.parse_args()

    try:
        import MetaTrader5 as mt5  # type: ignore
        import pandas as pd
    except ImportError as exc:
        print(f"需要 MetaTrader5 與 pandas:{exc}", file=sys.stderr)
        return 1
    if not mt5.initialize():
        print(f"MT5 初始化失敗:{mt5.last_error()}", file=sys.stderr)
        return 1
    try:
        bsym = args.broker_symbol or args.symbol
        mt5.symbol_select(bsym, True)
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
        rates = mt5.copy_rates_range(bsym, mt5.TIMEFRAME_H1, start, end)
        if rates is None or len(rates) == 0:
            print(f"沒有取得 {bsym} 的資料:{mt5.last_error()}", file=sys.stderr)
            return 1
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["volume"] = df["tick_volume"].astype(float)
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{args.symbol.upper()}_H1.csv"
        df[["time", "open", "high", "low", "close", "volume"]].to_csv(path, index=False)
        print(f"已寫入 {path}:{len(df)} 根,{df['time'].iloc[0]} → {df['time'].iloc[-1]}")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
