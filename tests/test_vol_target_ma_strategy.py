"""VolTargetMaStrategy: regime direction + vol-target position sizing + rebalancing.

Harness mirrors test_double_ma_strategy: MagicMock engine, a mocked ArrayManager
whose `close` array we control (to drive the trailing-vol estimate) and whose
`sma` we wire for the fast/slow regime, with shadowed order methods to assert
the exact lot counts the vol-target produces.

vol_window is set to 4 so the lot math is exact: close diffs of ±100 → std 100 →
per_lot_vol = 100 × contract_size(15) = 1500 → weight = target_vol(5000)/1500 =
3.33 → 3 lots.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from strategies.vol_target_ma_strategy import VolTargetMaStrategy


@pytest.fixture
def strategy():
    engine = MagicMock()
    s = VolTargetMaStrategy(engine, "TestVT", "ag2506.SHFE", {})
    s.am = MagicMock()
    s.am.inited = True
    s.vol_window = 4  # 5 closes → 4 diffs → deterministic std
    s.buy = MagicMock()
    s.sell = MagicMock()
    s.short = MagicMock()
    s.cover = MagicMock()
    s.sync_data = MagicMock()
    return s


def _bar(close: float = 5000.0) -> SimpleNamespace:
    return SimpleNamespace(close_price=close)


def _wire(strategy, closes, fast, slow) -> None:
    """Set the close array (drives vol) and the fast/slow MA endpoints (regime)."""
    strategy.am.close = np.array(closes, dtype=float)
    fast_arr = np.array([0.0] * 8 + [float(fast)])
    slow_arr = np.array([0.0] * 8 + [float(slow)])
    strategy.am.sma.side_effect = lambda n, array=False: (
        fast_arr if n == strategy.fast_window else slow_arr
    )


# Close arrays with known close-to-close std (population) over the last 4 diffs.
LOWVOL = [5000, 5100, 5000, 5100, 5000]  # diffs ±100 → std 100 → 3 lots
HIGHVOL = [5000, 5300, 5000, 5300, 5000]  # diffs ±300 → std 300 → 5000/4500=1.11 → 1 lot
TINYVOL = [5000, 5020, 5000, 5020, 5000]  # diffs ±20 → std 20 → weight 16.7 capped 4 → 4 lots


class TestSizing:
    def test_not_inited_no_orders(self, strategy):
        strategy.am.inited = False
        strategy.on_bar(_bar())
        strategy.buy.assert_not_called()
        strategy.short.assert_not_called()

    def test_flat_long_regime_opens_vol_sized_long(self, strategy):
        _wire(strategy, LOWVOL, fast=101, slow=100)
        strategy.pos = 0
        strategy.on_bar(_bar())
        strategy.buy.assert_called_once()
        assert strategy.buy.call_args.args[1] == 3  # vol-sized lots
        strategy.short.assert_not_called()

    def test_flat_short_regime_opens_vol_sized_short(self, strategy):
        _wire(strategy, LOWVOL, fast=99, slow=100)
        strategy.pos = 0
        strategy.on_bar(_bar())
        strategy.short.assert_called_once()
        assert strategy.short.call_args.args[1] == 3
        strategy.buy.assert_not_called()

    def test_higher_vol_fewer_lots(self, strategy):
        _wire(strategy, HIGHVOL, fast=101, slow=100)
        strategy.pos = 0
        strategy.on_bar(_bar())
        assert strategy.buy.call_args.args[1] == 1  # vs 3 in the low-vol case

    def test_leverage_cap_binds_in_tiny_vol(self, strategy):
        _wire(strategy, TINYVOL, fast=101, slow=100)
        strategy.pos = 0
        strategy.on_bar(_bar())
        assert strategy.buy.call_args.args[1] == 4  # capped at max_leverage

    def test_size_scale_multiplies_lots(self, strategy):
        strategy.size_scale = 2.0
        _wire(strategy, LOWVOL, fast=101, slow=100)
        strategy.pos = 0
        strategy.on_bar(_bar())
        assert strategy.buy.call_args.args[1] == 7  # round(3.33 × 2)

    def test_degenerate_vol_targets_flat(self, strategy):
        _wire(strategy, [5000, 5000, 5000, 5000, 5000], fast=101, slow=100)
        strategy.pos = 2  # currently long 2
        strategy.on_bar(_bar())
        strategy.sell.assert_called_once()
        assert strategy.sell.call_args.args[1] == 2  # flatten


class TestRebalance:
    def test_resize_up_same_direction_no_flip(self, strategy):
        """The defining VT behavior: re-size without a crossover. pos 2 → 3 long."""
        _wire(strategy, LOWVOL, fast=101, slow=100)
        strategy.pos = 2
        strategy.on_bar(_bar())
        strategy.buy.assert_called_once()
        assert strategy.buy.call_args.args[1] == 1  # 3 − 2
        strategy.sell.assert_not_called()
        strategy.short.assert_not_called()

    def test_resize_down_same_direction(self, strategy):
        """pos 3 long, vol rises → target 1 long → trim by selling 2 (no flip)."""
        _wire(strategy, HIGHVOL, fast=101, slow=100)
        strategy.pos = 3
        strategy.on_bar(_bar())
        strategy.sell.assert_called_once()
        assert strategy.sell.call_args.args[1] == 2
        strategy.short.assert_not_called()

    def test_flip_long_to_short_closes_then_opens(self, strategy):
        _wire(strategy, LOWVOL, fast=99, slow=100)  # short regime
        strategy.pos = 3  # currently long
        strategy.on_bar(_bar())
        strategy.sell.assert_called_once()
        assert strategy.sell.call_args.args[1] == 3  # close the long
        strategy.short.assert_called_once()
        assert strategy.short.call_args.args[1] == 3  # open the vol-sized short

    def test_flip_short_to_long_covers_then_buys(self, strategy):
        _wire(strategy, LOWVOL, fast=101, slow=100)  # long regime
        strategy.pos = -3  # currently short
        strategy.on_bar(_bar())
        strategy.cover.assert_called_once()
        assert strategy.cover.call_args.args[1] == 3
        strategy.buy.assert_called_once()
        assert strategy.buy.call_args.args[1] == 3

    def test_no_change_no_orders(self, strategy):
        _wire(strategy, LOWVOL, fast=101, slow=100)
        strategy.pos = 3  # already at target (long 3)
        strategy.on_bar(_bar())
        strategy.buy.assert_not_called()
        strategy.sell.assert_not_called()
        strategy.short.assert_not_called()
        strategy.cover.assert_not_called()
