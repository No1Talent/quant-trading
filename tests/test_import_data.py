"""_validate_csv, _build_bars/_build_ticks, import_csv/tick_to_database: batching, resume, failure isolation."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from vnpy.trader.constant import Exchange, Interval

from import_data import (
    _build_bars,
    _build_ticks,
    _validate_csv,
    _validate_tick_csv,
    import_csv_to_database,
    import_tick_csv_to_database,
)


def _make_df(rows: int = 3, **overrides) -> pd.DataFrame:
    n = rows
    base: dict = {
        "datetime": [f"2024-01-01 09:{i:02d}:00" for i in range(n)],
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
        "volume": [1000 + i * 100 for i in range(n)],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def _make_tick_df(rows: int = 3, **overrides) -> pd.DataFrame:
    n = rows
    base: dict = {
        "datetime": [f"2024-01-01 09:00:{i:02d}" for i in range(n)],
        "last_price": [100.0 + i for i in range(n)],
        "volume": [1000 + i * 100 for i in range(n)],
    }
    base.update(overrides)
    return pd.DataFrame(base)


class TestValidateCsv:
    def test_valid_df_passes(self):
        _validate_csv(_make_df())

    def test_missing_required_column_raises(self):
        df = _make_df().drop(columns=["volume"])
        with pytest.raises(ValueError, match="volume"):
            _validate_csv(df)

    def test_empty_df_raises(self):
        with pytest.raises(ValueError, match="空"):
            _validate_csv(pd.DataFrame(columns=_make_df().columns))

    def test_non_numeric_column_raises(self):
        df = _make_df()
        df["close"] = ["a", "b", "c"]
        with pytest.raises(ValueError, match="close"):
            _validate_csv(df)

    def test_optional_columns_not_required(self):
        # no turnover / open_interest — should pass without error
        _validate_csv(_make_df())


class TestBuildBars:
    FMT = "%Y-%m-%d %H:%M:%S"

    def test_count_matches_rows(self):
        bars = _build_bars(_make_df(3), "rb2510", Exchange.SHFE, Interval.MINUTE, self.FMT)
        assert len(bars) == 3

    def test_fields_populated_correctly(self):
        bar = _build_bars(_make_df(1), "rb2510", Exchange.SHFE, Interval.MINUTE, self.FMT)[0]
        assert bar.symbol == "rb2510"
        assert bar.exchange == Exchange.SHFE
        assert bar.open_price == 100.0
        assert bar.close_price == 100.5
        assert bar.volume == 1000.0
        assert bar.turnover == 0.0
        assert bar.open_interest == 0.0
        assert isinstance(bar.datetime, datetime)

    def test_optional_columns_populated_when_present(self):
        df = _make_df(1, turnover=[5000.0], open_interest=[200.0])
        bar = _build_bars(df, "rb2510", Exchange.SHFE, Interval.MINUTE, self.FMT)[0]
        assert bar.turnover == 5000.0
        assert bar.open_interest == 200.0

    def test_bad_row_skipped_rest_continue(self):
        df = _make_df(3)
        df.loc[1, "datetime"] = "NOT_A_DATE"
        bars = _build_bars(df, "rb2510", Exchange.SHFE, Interval.MINUTE, self.FMT)
        assert len(bars) == 2


class TestImportCsvToDatabase:
    @pytest.fixture
    def csv_file(self, tmp_path):
        path = tmp_path / "bars.csv"
        _make_df(10).to_csv(path, index=False)
        return path

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.get_bar_overview.return_value = []
        with patch("import_data.get_database", return_value=db):
            yield db

    def test_missing_csv_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            import_csv_to_database(tmp_path / "nope.csv", "rb2510", Exchange.SHFE)

    def test_returns_success_count(self, csv_file, mock_db):
        count = import_csv_to_database(csv_file, "rb2510", Exchange.SHFE)
        assert count == 10

    def test_progress_file_cleaned_on_success(self, csv_file, mock_db):
        import_csv_to_database(csv_file, "rb2510", Exchange.SHFE)
        assert not csv_file.with_suffix(".progress.json").exists()

    def test_resume_skips_completed_rows(self, csv_file, mock_db):
        progress_file = csv_file.with_suffix(".progress.json")
        progress_file.write_text(
            json.dumps({"completed_rows": 5, "total_rows": 10}),
            encoding="utf-8",
        )
        count = import_csv_to_database(csv_file, "rb2510", Exchange.SHFE, resume=True)
        assert count == 5  # only the remaining 5 rows processed

    def test_failed_batch_does_not_abort_import(self, csv_file, mock_db):
        call_count = 0

        def flaky_save(bars):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB transient error")

        mock_db.save_bar_data.side_effect = flaky_save
        # batch_size=5 → 2 batches; first fails, second succeeds
        count = import_csv_to_database(csv_file, "rb2510", Exchange.SHFE, batch_size=5)
        assert count == 5
        assert mock_db.save_bar_data.call_count == 2


class TestValidateTickCsv:
    def test_valid_df_passes(self):
        _validate_tick_csv(_make_tick_df())

    def test_missing_last_price_raises(self):
        df = _make_tick_df().drop(columns=["last_price"])
        with pytest.raises(ValueError, match="last_price"):
            _validate_tick_csv(df)

    def test_non_numeric_last_price_raises(self):
        df = _make_tick_df()
        df["last_price"] = ["a", "b", "c"]
        with pytest.raises(ValueError, match="last_price"):
            _validate_tick_csv(df)


class TestBuildTicks:
    FMT = "%Y-%m-%d %H:%M:%S"

    def test_count_matches_rows(self):
        ticks = _build_ticks(_make_tick_df(3), "rb2510", Exchange.SHFE, self.FMT)
        assert len(ticks) == 3

    def test_core_fields_populated(self):
        tick = _build_ticks(_make_tick_df(1), "rb2510", Exchange.SHFE, self.FMT)[0]
        assert tick.symbol == "rb2510"
        assert tick.exchange == Exchange.SHFE
        assert tick.last_price == 100.0
        assert tick.volume == 1000.0
        # 缺省可选列 → 0
        assert tick.turnover == 0.0
        assert tick.open_interest == 0.0
        assert tick.bid_price_1 == 0.0
        assert isinstance(tick.datetime, datetime)

    def test_optional_columns_populated_when_present(self):
        df = _make_tick_df(
            1,
            turnover=[5_000_000.0],
            open_interest=[200.0],
            last_volume=[3.0],
            bid_price_1=[99.9],
            ask_price_1=[100.1],
            bid_volume_1=[10.0],
            ask_volume_1=[12.0],
        )
        tick = _build_ticks(df, "rb2510", Exchange.SHFE, self.FMT)[0]
        assert tick.turnover == 5_000_000.0
        assert tick.open_interest == 200.0
        assert tick.last_volume == 3.0
        assert tick.bid_price_1 == 99.9
        assert tick.ask_volume_1 == 12.0

    def test_bad_row_skipped_rest_continue(self):
        df = _make_tick_df(3)
        df.loc[1, "datetime"] = "NOT_A_DATE"
        ticks = _build_ticks(df, "rb2510", Exchange.SHFE, self.FMT)
        assert len(ticks) == 2


class TestImportTickCsvToDatabase:
    @pytest.fixture
    def tick_csv(self, tmp_path):
        path = tmp_path / "ticks.csv"
        _make_tick_df(10).to_csv(path, index=False)
        return path

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.get_tick_overview.return_value = []
        with patch("import_data.get_database", return_value=db):
            yield db

    def test_missing_csv_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            import_tick_csv_to_database(tmp_path / "nope.csv", "rb2510", Exchange.SHFE)

    def test_returns_success_count_via_save_tick_data(self, tick_csv, mock_db):
        count = import_tick_csv_to_database(tick_csv, "rb2510", Exchange.SHFE)
        assert count == 10
        mock_db.save_tick_data.assert_called_once()
        mock_db.save_bar_data.assert_not_called()

    def test_progress_file_cleaned_on_success(self, tick_csv, mock_db):
        import_tick_csv_to_database(tick_csv, "rb2510", Exchange.SHFE)
        assert not tick_csv.with_suffix(".progress.json").exists()

    def test_resume_skips_completed_rows(self, tick_csv, mock_db):
        tick_csv.with_suffix(".progress.json").write_text(
            json.dumps({"completed_rows": 6, "total_rows": 10}),
            encoding="utf-8",
        )
        count = import_tick_csv_to_database(tick_csv, "rb2510", Exchange.SHFE, resume=True)
        assert count == 4
