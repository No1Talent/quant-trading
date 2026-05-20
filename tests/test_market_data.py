"""market_data: DB read, AkShare tail, composition, dedup, n_bars cap."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from utils.market_data import (
    BarRequest,
    _parse_vt_symbol,
    fetch_akshare_tail,
    get_recent_bars,
    load_db_bars,
)

SH = ZoneInfo("Asia/Shanghai")


def _fake_bar(dt: datetime, close: float = 100.0) -> MagicMock:
    """Mimic vn.py BarData — vn.py emits tz-aware Asia/Shanghai timestamps."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SH)
    m = MagicMock()
    m.datetime = dt
    m.open_price = close - 1
    m.high_price = close + 1
    m.low_price = close - 2
    m.close_price = close
    m.volume = 1000.0
    m.open_interest = 50000.0
    return m


class TestParseVtSymbol:
    def test_split(self):
        assert _parse_vt_symbol("rb2410.SHFE") == ("rb2410", "SHFE")

    def test_missing_dot_raises(self):
        with pytest.raises(ValueError, match="symbol.exchange"):
            _parse_vt_symbol("rb2410")


class TestLoadDbBars:
    def test_empty_returns_canonical_columns(self):
        fake_db = MagicMock()
        fake_db.load_bar_data.return_value = []
        df = load_db_bars(
            "rb2410.SHFE",
            "60m",
            datetime(2024, 1, 1),
            datetime(2025, 1, 1),
            db_factory=lambda: fake_db,
        )
        assert df.empty
        assert list(df.columns) == [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_returns_sorted_indexed_df(self):
        bars = [
            _fake_bar(datetime(2024, 1, 2, 10), close=102),
            _fake_bar(datetime(2024, 1, 1, 10), close=100),
        ]
        fake_db = MagicMock()
        fake_db.load_bar_data.return_value = bars
        df = load_db_bars(
            "rb2410.SHFE",
            "60m",
            datetime(2024, 1, 1),
            datetime(2025, 1, 1),
            db_factory=lambda: fake_db,
        )
        assert len(df) == 2
        assert df.index.is_monotonic_increasing
        assert df.iloc[0]["close"] == 100
        assert df.iloc[1]["close"] == 102
        assert df.attrs.get("source") == "db"

    def test_unknown_interval_raises(self):
        with pytest.raises(ValueError, match="unsupported interval"):
            load_db_bars(
                "rb2410.SHFE",
                "5m",
                datetime(2024, 1, 1),
                datetime(2025, 1, 1),
                db_factory=lambda: MagicMock(),
            )


class TestFetchAkshareTail:
    def test_60m_renames_hold_to_open_interest(self):
        fake = pd.DataFrame(
            {
                "datetime": ["2024-01-01 09:00:00", "2024-01-01 10:00:00"],
                "open": [100, 101],
                "high": [102, 103],
                "low": [99, 100],
                "close": [101, 102],
                "volume": [1000, 1100],
                "hold": [50000, 50100],
            }
        )
        df = fetch_akshare_tail("rb2410", "60m", fetcher=lambda s: fake)
        assert len(df) == 2
        assert "open_interest" in df.columns
        assert df.attrs["source"] == "akshare"
        # AkShare strings are naive; must be localised to Asia/Shanghai so
        # the concat in get_recent_bars dedups cleanly with tz-aware DB rows.
        assert df.index.tz is not None
        assert str(df.index.tz) == "Asia/Shanghai"

    def test_daily_synthesises_midnight_time(self):
        fake = pd.DataFrame(
            {
                "date": ["2024-01-01", "2024-01-02"],
                "open": [100, 101],
                "high": [102, 103],
                "low": [99, 100],
                "close": [101, 102],
                "volume": [1000, 1100],
            }
        )
        df = fetch_akshare_tail("rb2410", "1d", fetcher=lambda s: fake)
        assert len(df) == 2
        # All timestamps should be at 00:00:00
        assert all(t.hour == 0 and t.minute == 0 for t in df.index)
        # Missing 'hold' column → open_interest defaulted
        assert "open_interest" in df.columns

    def test_empty_returns_canonical(self):
        df = fetch_akshare_tail("rb2410", "60m", fetcher=lambda s: pd.DataFrame())
        assert df.empty
        assert list(df.columns) == [
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_unknown_interval_raises(self):
        with pytest.raises(ValueError, match="unsupported interval"):
            fetch_akshare_tail("rb2410", "5m", fetcher=lambda s: pd.DataFrame())


class TestGetRecentBars:
    def test_akshare_wins_on_overlap(self):
        # DB has 09:00 + 10:00; AkShare has 10:00 (different close) + 11:00.
        # The 10:00 close from AkShare should win after dedup.
        bars = [
            _fake_bar(datetime(2024, 1, 1, 9), close=100),
            _fake_bar(datetime(2024, 1, 1, 10), close=999),
        ]
        fake_db = MagicMock()
        fake_db.load_bar_data.return_value = bars
        fake_ak = pd.DataFrame(
            {
                "datetime": ["2024-01-01 10:00:00", "2024-01-01 11:00:00"],
                "open": [200, 201],
                "high": [202, 203],
                "low": [199, 200],
                "close": [201, 202],
                "volume": [2000, 2100],
                "hold": [60000, 60100],
            }
        )

        df = get_recent_bars(
            BarRequest("rb2410.SHFE", "60m", n_bars=5),
            db_factory=lambda: fake_db,
            ak_fetcher=lambda s: fake_ak,
        )
        assert len(df) == 3
        # Regression: before tz normalisation, naive AkShare + tz-aware DB
        # left two "10:00" rows (one for each tz state) instead of deduping.
        assert df.loc[datetime(2024, 1, 1, 10, tzinfo=SH)]["close"] == 201

    def test_n_bars_caps_output(self):
        fake_db = MagicMock()
        fake_db.load_bar_data.return_value = [_fake_bar(datetime(2024, 1, 1, h)) for h in range(20)]
        df = get_recent_bars(
            BarRequest("rb2410.SHFE", "60m", n_bars=5, use_realtime=False),
            db_factory=lambda: fake_db,
        )
        assert len(df) == 5

    def test_no_realtime_skips_akshare(self):
        # ak_fetcher omitted; default-import would fail / network in some envs.
        # If use_realtime=False, the branch must not touch akshare at all.
        fake_db = MagicMock()
        fake_db.load_bar_data.return_value = [_fake_bar(datetime(2024, 1, 1, 9))]
        df = get_recent_bars(
            BarRequest("rb2410.SHFE", "60m", n_bars=10, use_realtime=False),
            db_factory=lambda: fake_db,
        )
        assert len(df) == 1

    def test_empty_db_with_realtime_still_returns_ak(self):
        fake_db = MagicMock()
        fake_db.load_bar_data.return_value = []
        fake_ak = pd.DataFrame(
            {
                "datetime": ["2024-01-01 10:00:00"],
                "open": [100],
                "high": [102],
                "low": [99],
                "close": [101],
                "volume": [1000],
                "hold": [50000],
            }
        )
        df = get_recent_bars(
            BarRequest("rb2410.SHFE", "60m", n_bars=5),
            db_factory=lambda: fake_db,
            ak_fetcher=lambda s: fake_ak,
        )
        assert len(df) == 1
        assert df.iloc[0]["close"] == 101
