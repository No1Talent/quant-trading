"""Tests for research.overfit_stats — PSR, DSR, MinTRL, PBO (CSCV).

Pure closed-form math: no DB / vn.py dependency, runs in pytest-fast and CI.
Mixes exact anchors (where the formula has a known fixed point), an
independent from-scratch reference for PSR, round-trip consistency
(MinTRL ⇄ PSR), and the two robust PBO regimes (generalising ⇒ ~0,
pure noise ⇒ ~0.5).
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pytest
from scipy.stats import norm

from research.overfit_stats import (
    EULER_MASCHERONI,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    min_track_record_length,
    pbo_cscv,
    probabilistic_sharpe_ratio,
    sharpe_skew_kurt,
)

# --------------------------------------------------------------------------- #
# PSR
# --------------------------------------------------------------------------- #


def test_psr_zero_sr_is_half():
    """PSR(0) with an observed SR of exactly 0 must be 0.5 (z = 0)."""
    assert probabilistic_sharpe_ratio(0.0, 100, 0.0, 3.0, sr_benchmark=0.0) == pytest.approx(0.5)


def test_psr_at_benchmark_is_half():
    """PSR(benchmark = observed SR) is 0.5 by construction, for any moments."""
    assert probabilistic_sharpe_ratio(0.18, 250, -0.5, 6.0, sr_benchmark=0.18) == pytest.approx(0.5)


def test_psr_matches_independent_reference():
    """Match a from-scratch implementation of the Bailey-López de Prado formula."""
    sr, n, skew, kurt, bench = 0.1, 101, 0.3, 5.0, 0.0
    denom = math.sqrt(1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr)
    z = (sr - bench) * math.sqrt(n - 1) / denom
    expected = float(norm.cdf(z))
    assert probabilistic_sharpe_ratio(sr, n, skew, kurt, bench) == pytest.approx(
        expected, abs=1e-12
    )


def test_psr_monotonic_in_observed_sr():
    psr_lo = probabilistic_sharpe_ratio(0.05, 200, 0.0, 3.0)
    psr_hi = probabilistic_sharpe_ratio(0.15, 200, 0.0, 3.0)
    assert psr_hi > psr_lo


def test_psr_monotonic_in_sample_length():
    """More observations ⇒ more confident a positive SR is real."""
    psr_short = probabilistic_sharpe_ratio(0.1, 50, 0.0, 3.0)
    psr_long = probabilistic_sharpe_ratio(0.1, 5000, 0.0, 3.0)
    assert psr_long > psr_short


def test_psr_negative_skew_fat_tails_hurt():
    """Negative skew and excess kurtosis inflate the SR standard error,
    lowering PSR vs the Gaussian case at the same observed SR."""
    psr_normal = probabilistic_sharpe_ratio(0.12, 300, 0.0, 3.0)
    psr_ugly = probabilistic_sharpe_ratio(0.12, 300, -0.8, 8.0)
    assert psr_ugly < psr_normal


# --------------------------------------------------------------------------- #
# MinTRL
# --------------------------------------------------------------------------- #


def test_min_trl_roundtrip_with_psr():
    """PSR evaluated at exactly MinTRL observations recovers the target prob."""
    sr, skew, kurt, prob = 0.1, 0.2, 4.0, 0.95
    n_star = min_track_record_length(sr, skew, kurt, sr_benchmark=0.0, prob=prob)
    assert math.isfinite(n_star)
    recovered = probabilistic_sharpe_ratio(sr, int(round(n_star)), skew, kurt, 0.0)
    assert recovered == pytest.approx(prob, abs=2e-3)


def test_min_trl_infinite_without_edge():
    """No track-record length makes a SR at/below the benchmark significant."""
    assert min_track_record_length(0.0, 0.0, 3.0, sr_benchmark=0.0) == math.inf
    assert min_track_record_length(0.05, 0.0, 3.0, sr_benchmark=0.10) == math.inf


def test_min_trl_shrinks_with_stronger_sharpe():
    weak = min_track_record_length(0.05, 0.0, 3.0)
    strong = min_track_record_length(0.20, 0.0, 3.0)
    assert strong < weak


# --------------------------------------------------------------------------- #
# Expected-max Sharpe / DSR
# --------------------------------------------------------------------------- #


def test_expected_max_increases_with_trials():
    e_few = expected_max_sharpe(5, 1.0)
    e_many = expected_max_sharpe(500, 1.0)
    assert 0.0 < e_few < e_many


def test_expected_max_single_trial_no_deflation():
    """A single trial has no selection bias; SR₀ collapses to the null mean."""
    assert expected_max_sharpe(1, 1.0, sr_mean=0.0) == 0.0


def test_expected_max_zero_variance():
    assert expected_max_sharpe(100, 0.0) == 0.0


def test_expected_max_matches_formula():
    n_trials, var = 50, 0.25
    sigma = math.sqrt(var)
    g = EULER_MASCHERONI
    expected = sigma * (
        (1 - g) * norm.ppf(1 - 1.0 / n_trials) + g * norm.ppf(1 - 1.0 / (n_trials * math.e))
    )
    assert expected_max_sharpe(n_trials, var) == pytest.approx(expected, abs=1e-12)


def test_dsr_not_above_psr_zero():
    """Deflation can only reduce confidence: DSR = PSR(SR₀≥0) ≤ PSR(0)."""
    res = deflated_sharpe_ratio(0.12, 1000, 0.0, 3.0, n_trials=20, trial_sr_variance=0.02)
    assert res.dsr <= res.psr_zero + 1e-12
    assert res.sr0 > 0.0


def test_dsr_collapses_to_psr_zero_for_single_trial():
    res = deflated_sharpe_ratio(0.12, 1000, 0.0, 3.0, n_trials=1, trial_sr_variance=0.02)
    assert res.sr0 == 0.0
    assert res.dsr == pytest.approx(res.psr_zero)


def test_dsr_more_trials_more_deflation():
    a = deflated_sharpe_ratio(0.12, 1000, 0.0, 3.0, n_trials=5, trial_sr_variance=0.02)
    b = deflated_sharpe_ratio(0.12, 1000, 0.0, 3.0, n_trials=200, trial_sr_variance=0.02)
    assert b.dsr < a.dsr


# --------------------------------------------------------------------------- #
# sharpe_skew_kurt
# --------------------------------------------------------------------------- #


def test_sharpe_skew_kurt_on_normal_sample():
    rng = np.random.default_rng(7)
    x = rng.normal(0.05, 1.0, size=200_000)
    sr, skew, kurt, n = sharpe_skew_kurt(x)
    assert n == 200_000
    assert sr == pytest.approx(0.05, abs=0.01)
    assert skew == pytest.approx(0.0, abs=0.05)
    assert kurt == pytest.approx(3.0, abs=0.1)  # non-excess


def test_sharpe_skew_kurt_degenerate():
    sr, skew, kurt, n = sharpe_skew_kurt([1.0, 1.0, 1.0])
    assert math.isnan(sr) and math.isnan(skew) and math.isnan(kurt)
    assert n == 3


# --------------------------------------------------------------------------- #
# PBO (CSCV)
# --------------------------------------------------------------------------- #


def test_pbo_generalizing_matrix_is_low():
    """Config k has a monotonically higher mean and negligible noise, so the
    IS-best config is always the OOS-best — IS selection generalises perfectly,
    PBO ≈ 0."""
    rng = np.random.default_rng(1)
    T, N = 320, 8
    means = np.arange(N) * 0.01
    M = means[None, :] + rng.normal(0.0, 1e-4, size=(T, N))
    res = pbo_cscv(M, n_partitions=8)
    assert res.pbo < 0.05
    assert res.n_combos == math.comb(8, 4)


def test_pbo_pure_noise_is_near_half():
    """No config has a real edge ⇒ IS selection is a coin flip ⇒ PBO ≈ 0.5."""
    rng = np.random.default_rng(42)
    M = rng.normal(0.0, 1.0, size=(640, 10))
    res = pbo_cscv(M, n_partitions=10)
    assert 0.3 < res.pbo < 0.7
    assert res.n_configs == 10


def test_pbo_trims_to_divisible_length():
    rng = np.random.default_rng(0)
    M = rng.normal(size=(105, 4))  # 105 not divisible by 8
    res = pbo_cscv(M, n_partitions=8)
    assert res.n_obs_used == 104  # 13 rows/subset × 8
    assert res.logits.shape == (math.comb(8, 4),)


def test_pbo_rejects_odd_partitions():
    with pytest.raises(ValueError):
        pbo_cscv(np.zeros((100, 4)), n_partitions=7)


def test_pbo_rejects_single_config():
    with pytest.raises(ValueError):
        pbo_cscv(np.zeros((100, 1)), n_partitions=8)


def test_pbo_combination_count_matches_cscv():
    """n_combos must equal C(S, S/2) — the CSCV combination count."""
    rng = np.random.default_rng(3)
    M = rng.normal(size=(240, 6))
    res = pbo_cscv(M, n_partitions=12)
    assert res.n_combos == len(list(combinations(range(12), 6)))
