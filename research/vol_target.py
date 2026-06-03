"""Causal volatility targeting for daily PnL series.

M3.6b found that the H4 ensemble's per-split inverse-*train*-vol scaling is not
vol-stationary-safe: a weight calibrated on a fold's train window is applied to
its whole test window, so when test-period volatility diverges from train (AG
silver in 2026: ~16× the target), the position is grossly mis-sized. The result
is a +0.526 portfolio Sharpe sitting on kurtosis 63 — a few over-leveraged days
dominating the series.

This module replaces that with the standard CTA fix: re-size each day by a
*causal* trailing-volatility estimate, so the position adapts to vol regime
changes within the test window. The estimate uses only information available
before the day it sizes (rolling std through t-1), so it is implementable live.
"""

from __future__ import annotations

import pandas as pd

# Defaults chosen from the M3.6b robustness sweep (window 40-126 × cap 3-5 all
# gave portfolio Sharpe ~0.72-0.81 / kurtosis ~6-7). 63d ≈ one quarter; a 4×
# leverage cap is the midpoint of the safe band and bounds the position when
# trailing vol collapses.
DEFAULT_WINDOW = 63
DEFAULT_MAX_LEVERAGE = 4.0
DEFAULT_MIN_PERIODS = 20


def causal_vol_target(
    pnl: pd.Series,
    target_vol: float,
    window: int = DEFAULT_WINDOW,
    max_leverage: float = DEFAULT_MAX_LEVERAGE,
    min_periods: int = DEFAULT_MIN_PERIODS,
    return_weights: bool = False,
):
    """Re-scale a daily PnL series to a constant target volatility, causally.

    At day t the sizing weight is ``target_vol / σ_{t-1}`` where ``σ_{t-1}`` is
    the rolling ``window``-day std of PnL **through t-1** (``.shift(1)`` enforces
    no look-ahead). Weights are capped at ``max_leverage`` so a near-flat
    trailing window (σ → 0) cannot blow the position up. Warmup days without
    enough history for the rolling std are dropped.

    Args:
        pnl: daily PnL (or return) series, indexed by date.
        target_vol: desired daily std of the re-scaled series (same units as pnl).
        window: rolling-vol lookback in observations.
        max_leverage: upper bound on the per-day weight.
        min_periods: minimum observations before a vol estimate is produced.
        return_weights: if True, also return the per-day weight series.

    Returns:
        The re-scaled PnL series (warmup dropped), or ``(scaled, weights)`` if
        ``return_weights`` is True.
    """
    s = pd.Series(pnl).astype(float)
    # min_periods can never exceed the window (pandas rejects it); clamp so the
    # function degrades gracefully when called with a short window.
    effective_min_periods = min(min_periods, window)
    trailing_vol = s.rolling(window, min_periods=effective_min_periods).std().shift(1)
    # target/σ; σ==0 → inf, clipped to max_leverage; σ==NaN (warmup) → NaN.
    weight = (target_vol / trailing_vol).clip(upper=max_leverage)
    scaled = (s * weight).dropna()
    if return_weights:
        return scaled, weight.reindex(scaled.index)
    return scaled
