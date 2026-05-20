"""MaCrossRolloverGatedStrategy: DoubleMa entry gated by post-rollover window.

Two-layer logic to test:
  - Rollover counter: bars_since_rollover increments each bar, resets to 0 on
    H1.5 detection. `gated_now = bars_since_rollover <= post_roll_window`.
  - Entry/exit: same as DoubleMa, but entries only fire when gated_now is True;
    exits always fire (preserves trend-follow discipline).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from strategies.ma_cross_rollover_gated_strategy import MaCrossRolloverGatedStrategy


@pytest.fixture
def strategy():
    engine = MagicMock()
    s = MaCrossRolloverGatedStrategy(engine, "TestMaRoll", "i2410.DCE", {})
    s.am = MagicMock()
    s.am.inited = True
    s.buy = MagicMock()
    s.sell = MagicMock()
    s.short = MagicMock()
    s.cover = MagicMock()
    s.sync_data = MagicMock()
    return s


def _bar(
    open_: float = 100.0,
    close: float = 100.0,
    oi: float = 100_000.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        open_price=open_,
        close_price=close,
        open_interest=oi,
        high_price=max(open_, close),
        low_price=min(open_, close),
        volume=1000.0,
    )


def _configure_ma(strategy, fast: tuple[float, float], slow: tuple[float, float]) -> None:
    """Wire am.sma so fast/slow arrays have (prior, current) values at [-2], [-1]."""
    fast_arr = np.array([0.0] * 8 + [fast[0], fast[1]])
    slow_arr = np.array([0.0] * 8 + [slow[0], slow[1]])
    strategy.am.sma.side_effect = (
        lambda n, array=False: fast_arr if n == strategy.fast_window else slow_arr
    )


def _prime_for_rollover_today(strategy, prev_close: float = 100.0, prev_oi: float = 100_000.0):
    """Seed lag state so the next bar with OI jump + gap triggers detect_rollover."""
    strategy._prev_close = prev_close
    strategy._prev_oi = prev_oi


def _quiet_bar() -> SimpleNamespace:
    """Bar that does NOT trigger detect_rollover (tiny OI/gap deltas)."""
    return _bar(open_=100.05, close=100.10, oi=100_100.0)


def _rollover_bar_pos() -> SimpleNamespace:
    """Bar that DOES trigger detect_rollover with positive gap_sign."""
    return _bar(open_=102.0, close=102.5, oi=150_000.0)


class TestNotInited:
    def test_not_inited_no_orders(self, strategy):
        strategy.am.inited = False
        _prime_for_rollover_today(strategy)
        strategy.on_bar(_rollover_bar_pos())
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()
        # State must still be seeded so the next bar (post-init) sees correct prev_*
        assert strategy._prev_oi == 150_000.0
        assert strategy._prev_close == 102.5


class TestGatedEntries:
    def test_cross_over_within_window_opens_long(self, strategy):
        _prime_for_rollover_today(strategy)
        _configure_ma(strategy, fast=(99.0, 101.0), slow=(100.0, 100.0))
        strategy.pos = 0
        # Rollover bar resets counter to 0 → gated_now True
        strategy.on_bar(_rollover_bar_pos())
        strategy.buy.assert_called_once()
        strategy.short.assert_not_called()

    def test_cross_below_within_window_opens_short(self, strategy):
        _prime_for_rollover_today(strategy)
        _configure_ma(strategy, fast=(101.0, 99.0), slow=(100.0, 100.0))
        strategy.pos = 0
        strategy.on_bar(_rollover_bar_pos())
        strategy.short.assert_called_once()
        strategy.buy.assert_not_called()

    def test_cross_over_outside_window_blocked(self, strategy):
        """No recent rollover → bars_since_rollover huge → gated_now False → no entry."""
        _configure_ma(strategy, fast=(99.0, 101.0), slow=(100.0, 100.0))
        strategy.pos = 0
        # Quiet bar; sentinel _bars_since_rollover = 10**9 → +1 → still huge
        strategy.on_bar(_quiet_bar())
        strategy.buy.assert_not_called()

    def test_cross_below_outside_window_blocked(self, strategy):
        _configure_ma(strategy, fast=(101.0, 99.0), slow=(100.0, 100.0))
        strategy.pos = 0
        strategy.on_bar(_quiet_bar())
        strategy.short.assert_not_called()


class TestUngatedExits:
    """Exits must always fire on opposite cross, even if window has expired."""

    def test_short_covers_on_cross_over_even_when_window_expired(self, strategy):
        _configure_ma(strategy, fast=(99.0, 101.0), slow=(100.0, 100.0))
        strategy.pos = -1
        strategy.on_bar(_quiet_bar())  # no rollover → gated_now False
        strategy.cover.assert_called_once()
        # And no re-entry (pos still -1 in test; in prod safe_cover mutation would
        # take pos to 0, but the entry path requires gated_now=True anyway)
        strategy.buy.assert_not_called()

    def test_long_sells_on_cross_below_even_when_window_expired(self, strategy):
        _configure_ma(strategy, fast=(101.0, 99.0), slow=(100.0, 100.0))
        strategy.pos = 1
        strategy.on_bar(_quiet_bar())
        strategy.sell.assert_called_once()
        strategy.short.assert_not_called()


class TestSameBarFlipWithinWindow:
    def test_cross_over_within_window_covers_then_buys(self, strategy):
        _prime_for_rollover_today(strategy)
        _configure_ma(strategy, fast=(99.0, 101.0), slow=(100.0, 100.0))
        strategy.pos = -1

        def _flatten_on_cover(*_args, **_kwargs):
            strategy.pos = 0

        strategy.cover.side_effect = _flatten_on_cover
        strategy.on_bar(_rollover_bar_pos())
        strategy.cover.assert_called_once()
        strategy.buy.assert_called_once()

    def test_cross_below_within_window_sells_then_shorts(self, strategy):
        _prime_for_rollover_today(strategy)
        _configure_ma(strategy, fast=(101.0, 99.0), slow=(100.0, 100.0))
        strategy.pos = 1

        def _flatten_on_sell(*_args, **_kwargs):
            strategy.pos = 0

        strategy.sell.side_effect = _flatten_on_sell
        strategy.on_bar(_bar(open_=98.0, close=97.5, oi=150_000.0))
        strategy.sell.assert_called_once()
        strategy.short.assert_called_once()


class TestRolloverCounter:
    def test_rollover_resets_counter_to_zero(self, strategy):
        _prime_for_rollover_today(strategy)
        # Disable cross signals so we only test counter behavior
        _configure_ma(strategy, fast=(100.0, 100.0), slow=(100.0, 100.0))
        strategy._bars_since_rollover = 7
        strategy.on_bar(_rollover_bar_pos())
        assert strategy._bars_since_rollover == 0

    def test_quiet_bar_increments_counter(self, strategy):
        _configure_ma(strategy, fast=(100.0, 100.0), slow=(100.0, 100.0))
        strategy._bars_since_rollover = 2
        strategy.on_bar(_quiet_bar())
        assert strategy._bars_since_rollover == 3

    def test_counter_crosses_window_boundary_blocks_entry(self, strategy):
        """At bars_since_rollover == post_roll_window, last bar is still gated;
        the next bar increments past and gates out new entries.
        """
        _configure_ma(strategy, fast=(99.0, 101.0), slow=(100.0, 100.0))
        strategy.pos = 0
        # Set counter so post-increment lands exactly on post_roll_window (=5).
        strategy._bars_since_rollover = strategy.post_roll_window - 1
        strategy.on_bar(_quiet_bar())  # → 5 == post_roll_window → still gated
        strategy.buy.assert_called_once()
        # Next bar takes counter to 6 > 5 → gated out
        strategy.buy.reset_mock()
        strategy.pos = 0  # simulate flat again to retry entry
        strategy.on_bar(_quiet_bar())
        strategy.buy.assert_not_called()
