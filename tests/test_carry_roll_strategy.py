"""CarryRollStrategy signal logic: H1.5 rollover detection → directional carry capture.

The strategy uses no ArrayManager / SMA; alpha is mechanically defined by
``utils.rollover.detect_rollover`` plus a hold-day counter. So we only need
to drive bar-by-bar state (prev_oi / prev_close / bars_held) and assert the
expected safe_* call on each transition.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategies.carry_roll_strategy import CarryRollStrategy


@pytest.fixture
def strategy():
    engine = MagicMock()
    s = CarryRollStrategy(engine, "TestCarry", "i2410.DCE", {})
    # Stub vn.py order methods so safe_buy/sell/short/cover (which getattr them
    # off the strategy) route to MagicMocks the tests can assert against.
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


def _prime_prev(strategy, prev_close: float, prev_oi: float) -> None:
    """Seed the strategy's lag values so the next bar drives detect_rollover."""
    strategy._prev_close = prev_close
    strategy._prev_oi = prev_oi


class TestFlatNoRollover:
    def test_no_rollover_no_orders(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        # Both deltas tiny → no rollover
        strategy.on_bar(_bar(open_=100.1, close=100.2, oi=100_100.0))
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()

    def test_first_bar_seeds_state_no_orders(self, strategy):
        # prev_* both 0 from __init__ → detect_rollover early-returns False
        strategy.on_bar(_bar(open_=100.0, close=101.0, oi=100_000.0))
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()
        # state is now seeded for the next call
        assert strategy._prev_oi == 100_000.0
        assert strategy._prev_close == 101.0


class TestRolloverEntry:
    def test_rollover_positive_gap_opens_long(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        # OI jump 50% (>20%) + gap +2% (>0.3%) → rollover with gap_sign=+1
        strategy.on_bar(_bar(open_=102.0, close=102.5, oi=150_000.0))
        strategy.buy.assert_called_once()
        strategy.short.assert_not_called()

    def test_rollover_negative_gap_opens_short(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        # OI jump 50% + gap -2% → rollover with gap_sign=-1
        strategy.on_bar(_bar(open_=98.0, close=97.5, oi=150_000.0))
        strategy.short.assert_called_once()
        strategy.buy.assert_not_called()

    def test_rollover_blocked_when_already_holding(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        strategy.pos = 1  # already long
        strategy.bars_held = 1
        strategy.on_bar(_bar(open_=102.0, close=102.5, oi=150_000.0))
        # Holding → entry path skipped; bars_held still well under hold_days(5)
        # so no exit either
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()


class TestHoldDayExit:
    def test_long_exits_at_hold_days(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        strategy.pos = 1
        strategy.bars_held = strategy.hold_days - 1  # this bar increments to hold_days
        strategy.on_bar(_bar(open_=100.1, close=100.2, oi=100_100.0))  # quiet bar
        strategy.sell.assert_called_once()
        assert strategy.bars_held == 0

    def test_short_exits_at_hold_days(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        strategy.pos = -1
        strategy.bars_held = strategy.hold_days - 1
        strategy.on_bar(_bar(open_=100.1, close=100.2, oi=100_100.0))
        strategy.cover.assert_called_once()
        assert strategy.bars_held == 0

    def test_hold_below_threshold_no_exit(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        strategy.pos = 1
        strategy.bars_held = 1  # nowhere near hold_days(5)
        strategy.on_bar(_bar(open_=100.1, close=100.2, oi=100_100.0))
        strategy.sell.assert_not_called()
        assert strategy.bars_held == 2


class TestSameBarExitAndReentry:
    """Hold_days reached AND a fresh rollover on the same bar → exit then re-enter.

    The strategy explicitly orders "exit first, then check rollover" so the
    slot is freed for same-bar re-entry. This is the highest-value test —
    it pins the ordering invariant from the strategy's own docstring.
    """

    def test_long_exits_then_reenters_long_on_positive_rollover(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        strategy.pos = 1
        strategy.bars_held = strategy.hold_days - 1

        # Rollover bar with positive gap. After exit pos becomes 0 (modeled by
        # the test moving pos manually below — but for entry-after-exit logic
        # what matters is that buy() got called when bars_held reset).
        # CarryRoll's on_bar checks `if rollover and self.pos == 0` AFTER the
        # exit code — but it does NOT mutate self.pos in-process; it relies on
        # vn.py's order callback. In the test, MagicMocks don't mutate pos, so
        # we simulate the intended sequence by mutating pos inside a side_effect.
        def _flatten_on_sell(*_args, **_kwargs):
            strategy.pos = 0

        strategy.sell.side_effect = _flatten_on_sell
        strategy.on_bar(_bar(open_=102.0, close=102.5, oi=150_000.0))
        strategy.sell.assert_called_once()
        strategy.buy.assert_called_once()  # re-entry on positive rollover

    def test_short_exits_then_reenters_short_on_negative_rollover(self, strategy):
        _prime_prev(strategy, prev_close=100.0, prev_oi=100_000.0)
        strategy.pos = -1
        strategy.bars_held = strategy.hold_days - 1

        def _flatten_on_cover(*_args, **_kwargs):
            strategy.pos = 0

        strategy.cover.side_effect = _flatten_on_cover
        strategy.on_bar(_bar(open_=98.0, close=97.5, oi=150_000.0))
        strategy.cover.assert_called_once()
        strategy.short.assert_called_once()


class TestStateSeeding:
    def test_prev_state_updates_after_each_bar(self, strategy):
        strategy.on_bar(_bar(open_=100.0, close=101.0, oi=100_000.0))
        assert strategy._prev_oi == 100_000.0
        assert strategy._prev_close == 101.0
        strategy.on_bar(_bar(open_=101.0, close=102.0, oi=110_000.0))
        assert strategy._prev_oi == 110_000.0
        assert strategy._prev_close == 102.0
