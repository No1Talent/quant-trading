"""DoubleMaStrategy signal logic: golden cross, death cross, position-based order routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from strategies.double_ma_strategy import DoubleMaStrategy


@pytest.fixture
def strategy():
    engine = MagicMock()
    s = DoubleMaStrategy(engine, "TestDouble", "rb2510.SHFE", {})
    # Replace ArrayManager so we control MA values without feeding 100+ bars
    s.am = MagicMock()
    s.am.inited = True
    # Shadow order methods on the instance so we can assert call counts cleanly
    s.buy = MagicMock()
    s.sell = MagicMock()
    s.short = MagicMock()
    s.cover = MagicMock()
    s.sync_data = MagicMock()
    return s


def _bar(close: float = 100.0) -> SimpleNamespace:
    return SimpleNamespace(close_price=close)


def _configure_ma(strategy, fast: tuple[float, float], slow: tuple[float, float]) -> None:
    """Wire am.sma so fast/slow arrays have (prior, current) values at [-2], [-1]."""
    fast_arr = np.array([0.0] * 8 + [fast[0], fast[1]])
    slow_arr = np.array([0.0] * 8 + [slow[0], slow[1]])
    strategy.am.sma.side_effect = (
        lambda n, array=False: fast_arr if n == strategy.fast_window else slow_arr
    )


class TestDoubleMaStrategy:
    def test_not_inited_no_orders_placed(self, strategy):
        strategy.am.inited = False
        strategy.on_bar(_bar())
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()

    def test_golden_cross_from_flat_opens_long(self, strategy):
        # fast: 99 → 101 crosses above slow 100 → 100
        _configure_ma(strategy, fast=(99.0, 101.0), slow=(100.0, 100.0))
        strategy.pos = 0
        strategy.on_bar(_bar())
        strategy.buy.assert_called_once()
        strategy.short.assert_not_called()

    def test_death_cross_from_flat_opens_short(self, strategy):
        # fast: 101 → 99 crosses below slow 100 → 100
        _configure_ma(strategy, fast=(101.0, 99.0), slow=(100.0, 100.0))
        strategy.pos = 0
        strategy.on_bar(_bar())
        strategy.short.assert_called_once()
        strategy.buy.assert_not_called()

    def test_golden_cross_from_short_covers_then_buys(self, strategy):
        _configure_ma(strategy, fast=(99.0, 101.0), slow=(100.0, 100.0))
        strategy.pos = -1
        strategy.on_bar(_bar())
        strategy.cover.assert_called_once()
        strategy.buy.assert_called_once()

    def test_death_cross_from_long_sells_then_shorts(self, strategy):
        _configure_ma(strategy, fast=(101.0, 99.0), slow=(100.0, 100.0))
        strategy.pos = 1
        strategy.on_bar(_bar())
        strategy.sell.assert_called_once()
        strategy.short.assert_called_once()

    def test_no_cross_no_orders(self, strategy):
        # fast stays clearly above slow — no crossover event
        _configure_ma(strategy, fast=(105.0, 106.0), slow=(100.0, 101.0))
        strategy.pos = 0
        strategy.on_bar(_bar())
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()
