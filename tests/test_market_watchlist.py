"""market_watchlist: YAML loader, validation messages, default config sanity."""

from __future__ import annotations

from pathlib import Path

import pytest

from utils.market_watchlist import (
    DEFAULT_WATCHLIST_PATH,
    WatchItem,
    load_watchlist,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "wl.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestLoadWatchlist:
    def test_parses_well_formed_yaml(self, tmp_path):
        p = _write(
            tmp_path,
            """
watchlist:
  - vt_symbol: rb2501.SHFE
    name: 螺纹钢
    interval: 60m
    n_bars: 100
""",
        )
        items = load_watchlist(p)
        assert items == [WatchItem("rb2501.SHFE", "螺纹钢", "60m", 100)]

    def test_name_defaults_to_vt_symbol(self, tmp_path):
        p = _write(
            tmp_path,
            """
watchlist:
  - vt_symbol: rb2501.SHFE
    interval: 60m
    n_bars: 100
""",
        )
        items = load_watchlist(p)
        assert items[0].name == "rb2501.SHFE"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_watchlist(tmp_path / "nope.yaml")

    def test_empty_file_returns_empty_list(self, tmp_path):
        p = _write(tmp_path, "")
        assert load_watchlist(p) == []

    def test_missing_top_key_raises(self, tmp_path):
        p = _write(tmp_path, "other: stuff")
        with pytest.raises(ValueError, match="watchlist"):
            load_watchlist(p)

    def test_bad_interval_raises_with_value(self, tmp_path):
        p = _write(
            tmp_path,
            """
watchlist:
  - vt_symbol: rb2501.SHFE
    interval: 5m
    n_bars: 100
""",
        )
        with pytest.raises(ValueError, match="5m"):
            load_watchlist(p)

    def test_missing_dot_in_vt_symbol_raises(self, tmp_path):
        p = _write(
            tmp_path,
            """
watchlist:
  - vt_symbol: rb2501
    interval: 60m
    n_bars: 100
""",
        )
        with pytest.raises(ValueError, match="symbol.exchange"):
            load_watchlist(p)

    def test_missing_required_field_points_at_index(self, tmp_path):
        p = _write(
            tmp_path,
            """
watchlist:
  - vt_symbol: rb2501.SHFE
    interval: 60m
""",
        )
        with pytest.raises(ValueError, match="item 0"):
            load_watchlist(p)

    def test_non_positive_n_bars_raises(self, tmp_path):
        p = _write(
            tmp_path,
            """
watchlist:
  - vt_symbol: rb2501.SHFE
    interval: 60m
    n_bars: 0
""",
        )
        with pytest.raises(ValueError, match="positive"):
            load_watchlist(p)


class TestDefaultConfig:
    def test_shipped_yaml_parses(self):
        # The default config file ships with the repo; verify it stays valid
        # so a malformed edit shows up in CI before a refresh would fail.
        assert (
            DEFAULT_WATCHLIST_PATH.exists()
        ), f"default watchlist YAML missing: {DEFAULT_WATCHLIST_PATH}"
        items = load_watchlist()
        assert len(items) >= 1
        assert all(isinstance(it, WatchItem) for it in items)
