"""Market data composer for streamlit_market dashboard.

Combines vn.py SQLite (historical bars) with AkShare polling (latest tail)
into a unified DataFrame for chart rendering.

Read-only — never writes to DB. Designed for poll cadence ≥ 30 s; AkShare
quotes typically lag the exchange by ~15 s so faster polling buys nothing.

Why composition: the vn.py DB has long history but stops at whatever was
imported; AkShare has the latest tail but rate-limits longer queries.
Stitching the two gives "free" history without paying for an extra realtime
feed (we already maintain the DB for backtests).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

# vnpy and akshare are imported lazily inside functions so tests can inject
# fakes without paying their import cost (or needing them installed).

_CANONICAL_COLUMNS = ["open", "high", "low", "close", "volume", "open_interest"]

# vn.py stores bar timestamps as tz-aware Asia/Shanghai. AkShare strings are
# naive. We normalise both sources to this tz so they dedup cleanly on overlap.
_DEFAULT_TZ = "Asia/Shanghai"

# Map our string interval → (vn.py Interval enum name, AkShare period arg)
_INTERVAL_MAP: dict[str, tuple[str, str | None]] = {
    "1m": ("MINUTE", "1"),
    "60m": ("HOUR", "60"),
    "1d": ("DAILY", None),  # AkShare daily uses a separate fn
}


def _normalise_index_tz(df: pd.DataFrame) -> pd.DataFrame:
    """Force the index to tz-aware Asia/Shanghai. No-op on empty df."""
    if df.empty:
        return df
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        idx = pd.DatetimeIndex(idx)
    if idx.tz is None:
        idx = idx.tz_localize(_DEFAULT_TZ)
    else:
        idx = idx.tz_convert(_DEFAULT_TZ)
    df = df.copy()
    df.index = idx
    return df


@dataclass(frozen=True)
class BarRequest:
    """Describes which bars to load for the chart."""

    vt_symbol: str  # "rb2410.SHFE"
    interval: str  # "1m" / "60m" / "1d"
    n_bars: int = 200
    use_realtime: bool = True


def _parse_vt_symbol(vt_symbol: str) -> tuple[str, str]:
    if "." not in vt_symbol:
        raise ValueError(f"vt_symbol must be 'symbol.exchange', got {vt_symbol!r}")
    symbol, exchange = vt_symbol.split(".", 1)
    return symbol, exchange


def _empty_bars_df() -> pd.DataFrame:
    df = pd.DataFrame(columns=_CANONICAL_COLUMNS)
    df.index = pd.DatetimeIndex([], name="datetime")
    return df


def load_db_bars(
    vt_symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    db_factory: Callable[[], Any] | None = None,
) -> pd.DataFrame:
    """Read historical bars from the vn.py SQLite database.

    Returns DataFrame indexed by datetime with canonical columns. Empty when
    the DB has no rows for the (symbol, exchange, interval) tuple in range.
    """
    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.database import get_database

    symbol, exchange_str = _parse_vt_symbol(vt_symbol)
    if interval not in _INTERVAL_MAP:
        raise ValueError(f"unsupported interval: {interval}")
    vnpy_interval = Interval[_INTERVAL_MAP[interval][0]]
    exchange = Exchange[exchange_str]

    db = (db_factory or get_database)()
    bars = db.load_bar_data(symbol, exchange, vnpy_interval, start, end)
    if not bars:
        return _empty_bars_df()

    rows = [
        {
            "datetime": b.datetime,
            "open": b.open_price,
            "high": b.high_price,
            "low": b.low_price,
            "close": b.close_price,
            "volume": b.volume,
            "open_interest": b.open_interest,
        }
        for b in bars
    ]
    df = pd.DataFrame(rows).set_index("datetime").sort_index()
    df = _normalise_index_tz(df)
    df.attrs["source"] = "db"
    return df


def fetch_akshare_tail(
    symbol: str,
    interval: str,
    fetcher: Callable[[str], pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Pull latest bars from AkShare.

    `symbol` is the AkShare-style code (e.g. "rb2410", case-insensitive),
    NOT the vn.py vt_symbol. `fetcher` lets tests inject without importing
    akshare or hitting the network.
    """
    if interval not in _INTERVAL_MAP:
        raise ValueError(f"unsupported interval: {interval}")

    if fetcher is None:
        import akshare as ak  # type: ignore[import-not-found]

        if interval == "1d":
            fetcher = lambda s: ak.futures_zh_daily_sina(symbol=s.upper())  # noqa: E731
        else:
            period = _INTERVAL_MAP[interval][1]
            fetcher = lambda s: ak.futures_zh_minute_sina(  # noqa: E731
                symbol=s.upper(), period=period
            )

    raw = fetcher(symbol)
    if raw is None or len(raw) == 0:
        return _empty_bars_df()

    df = raw.rename(columns={"hold": "open_interest"})
    if "datetime" not in df.columns and "date" in df.columns:
        df = df.rename(columns={"date": "datetime"})
        df["datetime"] = df["datetime"].astype(str) + " 00:00:00"

    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()

    keep = [c for c in _CANONICAL_COLUMNS if c in df.columns]
    df = df[keep]
    if "open_interest" not in df.columns:
        df["open_interest"] = 0.0
    df = _normalise_index_tz(df)
    df.attrs["source"] = "akshare"
    return df


def get_recent_bars(
    request: BarRequest,
    db_factory: Callable[[], Any] | None = None,
    ak_fetcher: Callable[[str], pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Compose DB history + AkShare tail; return the last `n_bars` rows.

    AkShare wins on overlap — the live feed is fresher than whatever was
    last imported. If AkShare is empty / errored, DB-only is still returned.
    """
    symbol, _ = _parse_vt_symbol(request.vt_symbol)

    end = datetime.now() + timedelta(days=1)
    if request.interval == "1m":
        start = end - timedelta(days=30)
    elif request.interval == "60m":
        start = end - timedelta(days=365 * 3)
    else:  # "1d"
        start = end - timedelta(days=365 * 10)

    df_db = load_db_bars(request.vt_symbol, request.interval, start, end, db_factory=db_factory)

    if request.use_realtime:
        df_ak = fetch_akshare_tail(symbol, request.interval, fetcher=ak_fetcher)
        # Filter empties before concat — pandas ≥ 2.2 deprecates auto-skipping
        # empty frames when inferring result dtype.
        frames = [d for d in (df_db, df_ak) if not d.empty]
        if not frames:
            df = df_db
        elif len(frames) == 1:
            df = frames[0]
        else:
            # Concat order matters: ak last → wins under keep="last" dedup
            df = pd.concat(frames)
            df = df[~df.index.duplicated(keep="last")].sort_index()
    else:
        df = df_db

    if df.empty:
        return df

    return df.tail(request.n_bars)
