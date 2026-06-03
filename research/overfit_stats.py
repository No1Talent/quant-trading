"""Backtest-overfitting statistics: PSR, DSR, MinTRL, and PBO (CSCV).

Closed-form selection-bias / non-normality corrections from the Bailey &
López de Prado line of work. These answer the question our WFA + CPCV-PWF
pipeline raises but does not close: *given that we searched a grid of N
configurations and report a Sharpe, how much of that Sharpe is luck?*

Four estimators, all implemented from the papers (no external overfit lib):

  - `probabilistic_sharpe_ratio` (PSR) — Bailey & López de Prado (2012),
    "The Sharpe Ratio Efficient Frontier", J. of Risk. P(true SR > benchmark)
    given the *observed* SR, sample length, skew and kurtosis. PSR(0) is the
    "is it even positive after accounting for short, fat-tailed samples?" gate.

  - `min_track_record_length` (MinTRL) — same paper. How many observations are
    needed before PSR(benchmark) ≥ prob. If MinTRL > len(track record), the
    Sharpe is not yet statistically distinguishable from the benchmark.

  - `deflated_sharpe_ratio` (DSR) — Bailey & López de Prado (2014), "The
    Deflated Sharpe Ratio". PSR evaluated against SR₀ = the *expected maximum*
    Sharpe under the null that all N trials have true SR = 0. Deflates the
    headline number for the number of trials (grid size) and their dispersion.

  - `pbo_cscv` (PBO) — Bailey, Borwein, López de Prado & Zhu (2017), "The
    Probability of Backtest Overfitting", J. of Computational Finance, via
    Combinatorially-Symmetric Cross-Validation (AFML §11.4). Fraction of
    train/test combinations in which the in-sample-best configuration lands
    below the out-of-sample median — i.e. the rate at which IS selection fails
    to generalise.

Conventions (READ THIS):
  * All Sharpe inputs are **per-observation** (NOT annualised). For a daily
    series, divide an annualised Sharpe by sqrt(252), or just feed the raw
    mean/std of daily returns. Use `sharpe_skew_kurt` to extract a consistent
    (sr, skew, kurt, n) tuple from a return/PnL series.
  * `kurt` is the **non-excess** kurtosis (a normal distribution has kurt = 3),
    matching the paper's γ₄. `sharpe_skew_kurt` returns it that way.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

import numpy as np
from scipy.stats import norm

# Euler–Mascheroni constant (γ), used in the expected-maximum-of-N-Gaussians
# approximation behind the Deflated Sharpe Ratio.
EULER_MASCHERONI = 0.5772156649015329


def sharpe_skew_kurt(returns: Sequence[float]) -> tuple[float, float, float, int]:
    """Extract (per-observation Sharpe, skew, non-excess kurtosis, n) from a
    return or PnL series. Sharpe is scale-invariant, so a PnL series in currency
    units gives the same Sharpe/skew/kurt as the corresponding return series
    (as long as capital is constant) — which is exactly how the H4 panel stores
    daily net_pnl.
    """
    a = np.asarray(returns, dtype=float)
    a = a[np.isfinite(a)]
    n = a.size
    if n < 2:
        return float("nan"), float("nan"), float("nan"), n
    mean = a.mean()
    std = a.std(ddof=1)
    if std <= 0:
        return float("nan"), float("nan"), float("nan"), n
    sr = mean / std
    # Population moments (matching the moment definitions in the PSR paper).
    z = (a - mean) / a.std(ddof=0)
    skew = float((z**3).mean())
    kurt = float((z**4).mean())  # non-excess: normal ≈ 3.0
    return float(sr), skew, kurt, int(n)


def _psr_denominator(sr: float, skew: float, kurt: float) -> float:
    """√(1 − γ₃·SR + ((γ₄−1)/4)·SR²) — the standard error scaling of the Sharpe
    estimator under non-normal returns (Mertens / Bailey-López de Prado)."""
    val = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    # Numerical floor: the quantity is a variance and must stay positive; for
    # extreme skew/kurt with large SR it can dip below 0, which signals the
    # asymptotic approximation is breaking down.
    return math.sqrt(max(val, 1e-12))


def probabilistic_sharpe_ratio(
    sr: float, n_obs: int, skew: float, kurt: float, sr_benchmark: float = 0.0
) -> float:
    """PSR(sr_benchmark) = P(true SR > sr_benchmark).

    All Sharpe arguments are per-observation. `kurt` is non-excess (normal = 3).
    Returns a probability in [0, 1]. PSR(sr_benchmark = sr) == 0.5 by construction.
    """
    if not (math.isfinite(sr) and n_obs > 1):
        return float("nan")
    denom = _psr_denominator(sr, skew, kurt)
    z = (sr - sr_benchmark) * math.sqrt(n_obs - 1) / denom
    return float(norm.cdf(z))


def min_track_record_length(
    sr: float, skew: float, kurt: float, sr_benchmark: float = 0.0, prob: float = 0.95
) -> float:
    """Minimum number of observations for PSR(sr_benchmark) ≥ prob.

    Returns +inf if the observed Sharpe does not exceed the benchmark (no track
    record length makes a non-edge significant). Compare against the actual
    sample length: MinTRL ≤ n ⇒ already significant at `prob`.
    """
    if not math.isfinite(sr) or sr <= sr_benchmark:
        return float("inf")
    denom_term = _psr_denominator(sr, skew, kurt) ** 2
    z = float(norm.ppf(prob))
    return 1.0 + denom_term * (z / (sr - sr_benchmark)) ** 2


def expected_max_sharpe(n_trials: int, trial_sr_variance: float, sr_mean: float = 0.0) -> float:
    """Expected maximum of `n_trials` i.i.d. Sharpe estimates with the given
    cross-trial variance, under the null that the mean Sharpe is `sr_mean`
    (typically 0). This is SR₀, the deflation benchmark in the DSR.

    Uses the extreme-value approximation E[max] ≈ μ + σ·[(1−γ)·Φ⁻¹(1 − 1/N) +
    γ·Φ⁻¹(1 − 1/(N·e))] from Bailey & López de Prado (2014).
    """
    if n_trials < 2:
        # With a single trial there is no selection bias to deflate.
        return sr_mean
    sigma = math.sqrt(max(trial_sr_variance, 0.0))
    g = EULER_MASCHERONI
    term = (1.0 - g) * norm.ppf(1.0 - 1.0 / n_trials) + g * norm.ppf(
        1.0 - 1.0 / (n_trials * math.e)
    )
    return sr_mean + sigma * float(term)


@dataclass
class DSRResult:
    dsr: float  # PSR evaluated at SR₀ — P(true SR > expected max under null)
    sr0: float  # expected maximum Sharpe under the null (per-observation)
    psr_zero: float  # PSR(0) for reference (no multiple-testing deflation)
    sr: float
    n_obs: int
    n_trials: int
    trial_sr_variance: float


def deflated_sharpe_ratio(
    sr: float,
    n_obs: int,
    skew: float,
    kurt: float,
    n_trials: int,
    trial_sr_variance: float,
) -> DSRResult:
    """Deflated Sharpe Ratio.

    DSR = PSR(SR₀) where SR₀ = expected maximum Sharpe across `n_trials`
    under the null. A DSR near 1 means the observed Sharpe is unlikely to be a
    product of selecting the best of N trials; near 0.5 or below means it is
    statistically indistinguishable from the luckiest of N coin-flips.

    `trial_sr_variance` is the variance of the per-observation Sharpe ratios
    across the configurations actually searched.
    """
    sr0 = expected_max_sharpe(n_trials, trial_sr_variance, sr_mean=0.0)
    dsr = probabilistic_sharpe_ratio(sr, n_obs, skew, kurt, sr_benchmark=sr0)
    psr0 = probabilistic_sharpe_ratio(sr, n_obs, skew, kurt, sr_benchmark=0.0)
    return DSRResult(
        dsr=dsr,
        sr0=sr0,
        psr_zero=psr0,
        sr=sr,
        n_obs=n_obs,
        n_trials=n_trials,
        trial_sr_variance=trial_sr_variance,
    )


@dataclass
class PBOResult:
    pbo: float  # P(IS-best config underperforms OOS median)
    n_combos: int
    logit_mean: float
    logit_median: float
    n_partitions: int
    n_configs: int
    n_obs_used: int
    logits: np.ndarray


def _group_sharpe(
    sub_sum: np.ndarray, sub_sumsq: np.ndarray, rows_per_sub: int, mask: np.ndarray
) -> np.ndarray:
    """Per-config Sharpe over the pooled rows of the masked subsets.

    sub_sum / sub_sumsq are (S, N) arrays of per-subset Σx and Σx² per config;
    `mask` selects the subsets in this group. Vectorised over the N configs.
    """
    cnt = rows_per_sub * int(mask.sum())
    s = sub_sum[mask].sum(axis=0)
    ss = sub_sumsq[mask].sum(axis=0)
    mean = s / cnt
    var = ss / cnt - mean**2
    std = np.sqrt(np.maximum(var, 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        sr = np.where(std > 0, mean / std, 0.0)
    return sr


def pbo_cscv(perf_matrix: np.ndarray, n_partitions: int = 16) -> PBOResult:
    """Probability of Backtest Overfitting via Combinatorially-Symmetric CV.

    `perf_matrix`: shape (T, N) of per-observation returns/PnL for N candidate
    configurations over T aligned periods (e.g. the 9 DoubleMa grid combos'
    daily net_pnl). The matrix is partitioned into `n_partitions` (S, must be
    even) contiguous blocks; for every way of choosing S/2 blocks as in-sample
    (the rest out-of-sample) we:
        1. pick n* = argmax in-sample Sharpe,
        2. find n*'s rank among all configs by out-of-sample Sharpe,
        3. map relative rank ω = rank/(N+1) to logit λ = ln(ω/(1−ω)).
    PBO = fraction of combinations with λ ≤ 0 (IS-best at/below OOS median).

    Trailing rows are trimmed so T divides evenly into S blocks.
    """
    M = np.asarray(perf_matrix, dtype=float)
    if M.ndim != 2:
        raise ValueError("perf_matrix must be 2D (T observations × N configs)")
    T, N = M.shape
    S = n_partitions
    if S % 2 != 0:
        raise ValueError("n_partitions must be even (CSCV splits into two equal halves)")
    if T < S:
        raise ValueError(f"need at least n_partitions={S} rows, got {T}")
    if N < 2:
        raise ValueError("need at least 2 configurations to rank")

    rows_per_sub = T // S
    usable = rows_per_sub * S
    M = M[:usable]
    subsets = M.reshape(S, rows_per_sub, N)
    sub_sum = subsets.sum(axis=1)  # (S, N)
    sub_sumsq = (subsets**2).sum(axis=1)  # (S, N)

    half = S // 2
    logits: list[float] = []
    for is_idx in combinations(range(S), half):
        is_mask = np.zeros(S, dtype=bool)
        is_mask[list(is_idx)] = True
        oos_mask = ~is_mask

        is_sr = _group_sharpe(sub_sum, sub_sumsq, rows_per_sub, is_mask)
        oos_sr = _group_sharpe(sub_sum, sub_sumsq, rows_per_sub, oos_mask)

        n_star = int(np.argmax(is_sr))
        # Relative OOS rank of the IS winner (1 = worst, N = best).
        order = np.argsort(oos_sr, kind="stable")
        rank = int(np.nonzero(order == n_star)[0][0]) + 1
        omega = rank / (N + 1.0)
        omega = min(max(omega, 1e-6), 1.0 - 1e-6)
        logits.append(math.log(omega / (1.0 - omega)))

    arr = np.asarray(logits, dtype=float)
    return PBOResult(
        pbo=float((arr <= 0).mean()),
        n_combos=int(arr.size),
        logit_mean=float(arr.mean()),
        logit_median=float(np.median(arr)),
        n_partitions=S,
        n_configs=N,
        n_obs_used=int(usable),
        logits=arr,
    )
