"""AkShare → CSV (vn.py-compatible) for single-contract historical bars.

Designed for Layer ② research: pull a delisted single-lifecycle futures contract,
write to CSV in a format that import_data.py can consume unchanged, then optionally
import to the vn.py database in one step.

V1 scope: 60min bars only. Daily can be added by extending _FETCHERS.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import akshare as ak
import pandas as pd

logger = logging.getLogger("data_fetcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "bar"


def fetch_60min(symbol: str) -> pd.DataFrame:
    """Pull 60min bars from AkShare. Symbol must be uppercase (e.g. 'RB2410')."""
    df = ak.futures_zh_minute_sina(symbol=symbol.upper(), period="60")
    if df is None or len(df) == 0:
        raise RuntimeError(f"AkShare returned empty data for {symbol} 60min")

    # AkShare columns: datetime, open, high, low, close, volume, hold
    df = df.rename(columns={"hold": "open_interest"})
    return df[["datetime", "open", "high", "low", "close", "volume", "open_interest"]]


def fetch_daily(symbol: str) -> pd.DataFrame:
    """Pull daily bars from AkShare. Returns columns matching import_data.py spec.

    AkShare daily 'date' is just YYYY-MM-DD; we append '00:00:00' so the existing
    import_data.py datetime_format ("%Y-%m-%d %H:%M:%S") parses without changes.
    """
    df = ak.futures_zh_daily_sina(symbol=symbol.upper())
    if df is None or len(df) == 0:
        raise RuntimeError(f"AkShare returned empty data for {symbol} daily")

    # AkShare daily columns: date, open, high, low, close, volume, hold, settle
    df = df.rename(columns={"date": "datetime", "hold": "open_interest"})
    df["datetime"] = df["datetime"].astype(str) + " 00:00:00"
    return df[["datetime", "open", "high", "low", "close", "volume", "open_interest"]]


def save_csv(df: pd.DataFrame, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
    logger.info("Saved %d rows → %s", len(df), csv_path)


def fetch_and_save(symbol: str, timeframe: str = "60min") -> Path:
    if timeframe == "60min":
        df = fetch_60min(symbol)
    elif timeframe == "daily":
        df = fetch_daily(symbol)
    else:
        raise NotImplementedError(f"timeframe={timeframe} not supported")

    logger.info("Fetching %s %s from AkShare...", symbol, timeframe)
    logger.info("Got %d bars: %s ~ %s", len(df), df["datetime"].iloc[0], df["datetime"].iloc[-1])

    csv_path = DATA_DIR / f"{symbol.lower()}_{timeframe}.csv"
    save_csv(df, csv_path)
    return csv_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch single-contract historical bars from AkShare."
    )
    parser.add_argument("symbol", help="Contract code, e.g. RB2410")
    parser.add_argument("--timeframe", default="60min", choices=["60min", "daily"])
    parser.add_argument(
        "--import-to-db",
        action="store_true",
        help="After saving CSV, also import to vn.py database (requires vn.py installed).",
    )
    parser.add_argument(
        "--exchange",
        default="SHFE",
        help="Exchange enum value for DB import (SHFE/DCE/CZCE/INE/CFFEX). Required if --import-to-db.",
    )
    parser.add_argument(
        "--store-as",
        default=None,
        help="Override DB symbol on import. Use to avoid collisions when storing "
        "AkShare continuous symbols (e.g. RB0 -> rb_continuous). CSV path always "
        "uses the original fetched symbol.",
    )
    args = parser.parse_args()

    csv_path = fetch_and_save(args.symbol, args.timeframe)

    if args.import_to_db:
        from vnpy.trader.constant import Exchange, Interval

        from import_data import import_csv_to_database

        interval = Interval.HOUR if args.timeframe == "60min" else Interval.DAILY
        db_symbol = args.store_as if args.store_as else args.symbol.lower()
        import_csv_to_database(
            csv_path=csv_path,
            symbol=db_symbol,
            exchange=Exchange[args.exchange],
            interval=interval,
            batch_size=5000,
            resume=False,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
