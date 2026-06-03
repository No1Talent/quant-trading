"""Tests for research.vol_target.causal_vol_target.

Pure pandas/numpy — no DB / vn.py / scipy, runs in pytest-fast and CI. The
load-bearing property is *causality* (the day-t weight must not see day-t PnL);
the rest assert it actually targets the vol, respects the cap, and de-levers in
high-vol regimes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.vol_target import causal_vol_target


def _series(values):
    idx = pd.date_range("2020-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


def test_no_lookahead_weight_independent_of_same_day_pnl():
    """The day-k weight must depend only on PnL before k. Perturbing pnl[k]
    (and beyond) must leave every weight up to and including k unchanged."""
    rng = np.random.default_rng(0)
    base = _series(rng.normal(0, 1000, 300))
    bumped = base.copy()
    k = 200
    bumped.iloc[k] *= 50  # giant spike at day k

    _, w_base = causal_vol_target(base, 5000, window=30, return_weights=True)
    _, w_bumped = causal_vol_target(bumped, 5000, window=30, return_weights=True)

    common = w_base.index.intersection(w_bumped.index)
    kth = base.index[k]
    upto_k = common[common <= kth]
    # Identical through day k (sizing uses only the past)...
    pd.testing.assert_series_equal(w_base[upto_k], w_bumped[upto_k])
    # ...and the spike does move later weights (sanity: the test is not vacuous).
    after = common[common > kth]
    assert not np.allclose(w_base[after].values, w_bumped[after].values)


def test_targets_requested_vol_on_stationary_series():
    rng = np.random.default_rng(1)
    pnl = _series(rng.normal(0, 3000, 4000))
    scaled = causal_vol_target(pnl, target_vol=5000, window=63, max_leverage=1e9)
    # Generous tolerance: a causal estimate tracks but never perfectly hits.
    assert scaled.std() == pytest.approx(5000, rel=0.15)


def test_weights_never_exceed_cap():
    pnl = _series([0.0, 0.0, 0.0, 0.0, 0.0, 1.0, -1.0, 2.0, -2.0, 1.0] * 20)
    _, w = causal_vol_target(pnl, 5000, window=10, max_leverage=3.0, return_weights=True)
    assert (w <= 3.0 + 1e-12).all()
    assert np.isfinite(w).all()


def test_zero_trailing_vol_hits_cap_not_inf():
    # First window is flat (σ=0) → weight would be inf without the cap.
    pnl = _series([0.0] * 30 + list(np.random.default_rng(2).normal(0, 100, 70)))
    _, w = causal_vol_target(pnl, 5000, window=15, max_leverage=5.0, return_weights=True)
    assert np.isfinite(w).all()
    assert w.max() == pytest.approx(5.0)


def test_warmup_dropped():
    pnl = _series(np.random.default_rng(3).normal(0, 1000, 100))
    scaled = causal_vol_target(pnl, 5000, window=20, min_periods=20)
    # Need min_periods obs for the rolling std, then shift(1) drops one more.
    assert len(scaled) == 100 - 20
    assert scaled.index[0] == pnl.index[20]


def test_de_levers_in_high_vol_regime():
    """A calm regime should get larger weights than a turbulent one."""
    rng = np.random.default_rng(4)
    calm = rng.normal(0, 500, 200)
    wild = rng.normal(0, 5000, 200)
    pnl = _series(np.concatenate([calm, wild]))
    _, w = causal_vol_target(pnl, 5000, window=40, max_leverage=1e9, return_weights=True)
    calm_w = w.iloc[60:190].mean()  # well inside calm regime
    wild_w = w.iloc[260:390].mean()  # well inside wild regime
    assert calm_w > wild_w


def test_scaled_series_is_input_times_weight():
    pnl = _series(np.random.default_rng(5).normal(0, 1000, 150))
    scaled, w = causal_vol_target(pnl, 5000, window=30, return_weights=True)
    pd.testing.assert_series_equal(scaled, (pnl.reindex(scaled.index) * w), check_names=False)
