"""Tests for utils/signal_core.py.

These pin the pure-Python signal cores to hand-computed expectations and to the
exact entry/exit semantics of strategies/*.py. They need neither vnpy nor TA-Lib,
so they run in CI on Linux.
"""

import numpy as np

from utils.signal_core import (
    Action,
    replay_boll_reversal,
    replay_dataframe,
    replay_donchian,
    replay_double_ma,
    rolling_std_pop,
    sma,
)


# ---- indicators ----------------------------------------------------------
def test_sma_matches_manual():
    out = sma([5.0, 5.0, 5.0, 8.0, 9.0], 2)
    assert np.isnan(out[0])
    assert out[1] == 5.0
    assert out[3] == 6.5
    assert out[4] == 8.5


def test_rolling_std_pop_is_population():
    out = rolling_std_pop([10.0, 10.0, 13.0], 3)
    # mean 11, var = (1+1+4)/3 = 2, std = sqrt(2)
    assert abs(out[2] - np.sqrt(2)) < 1e-12


# ---- double MA -----------------------------------------------------------
def test_double_ma_golden_cross_goes_long():
    r = replay_double_ma([5, 5, 5, 5, 8, 9], fast_window=2, slow_window=3, min_bars=3)
    assert len(r.actions) == 1
    a = r.actions[0]
    assert (a.side, a.index, a.price) == ("buy", 4, 8.0)
    assert r.stance == 1


def test_double_ma_death_cross_goes_short():
    r = replay_double_ma([9, 9, 9, 9, 6, 5], fast_window=2, slow_window=3, min_bars=3)
    assert len(r.actions) == 1
    a = r.actions[0]
    assert (a.side, a.index) == ("short", 4)
    assert r.stance == -1


# ---- donchian ------------------------------------------------------------
def test_donchian_breakout_then_flip():
    high = [10, 10, 10, 10, 13, 13, 13]
    low = [9, 9, 9, 9, 9, 11, 8]
    close = [9.5, 9.5, 9.5, 9.5, 12, 12.5, 8.5]
    r = replay_donchian(high, low, close, entry_window=3, exit_window=2, min_bars=3)
    sides = [(a.side, a.index) for a in r.actions]
    assert sides[0] == ("buy", 4)
    # last bar both closes the long and flips short
    assert sides[-2:] == [("sell", 6), ("short", 6)]
    assert r.stance == -1
    assert [a.side for a in r.last_bar_actions] == ["sell", "short"]


# ---- bollinger reversal --------------------------------------------------
def test_boll_reversal_fade_and_revert():
    r = replay_boll_reversal([10, 10, 10, 13, 10], boll_window=3, boll_dev=1.0, min_bars=3)
    sides = [(a.side, a.index) for a in r.actions]
    assert sides == [("short", 3), ("cover", 4)]
    assert r.stance == 0
    assert r.fired_on_last_bar  # cover happened on the final bar


# ---- dataframe dispatcher ------------------------------------------------
def test_replay_dataframe_double_ma():
    import pandas as pd

    df = pd.DataFrame(
        {
            "datetime": pd.date_range("2024-01-01", periods=6, freq="D"),
            "close": [5, 5, 5, 5, 8, 9],
        }
    )
    r = replay_dataframe("double_ma", df, {"fast_window": 2, "slow_window": 3, "min_bars": 3})
    assert r.stance == 1
    assert r.actions[0].side == "buy"
    assert isinstance(r.actions[0], Action)
