"""M2.5: Deep factor evaluation - IC time-stability + multi-factor combination.

Two analyses on top of the M2 baseline. Motivation: M2 v1 found every single
factor has |IC IR| < 0.11 — no candidate above the 0.3 soft threshold. Two
hypotheses for "the signal exists but is buried":

(A) Time-stability of IC: maybe some factors have real edge in specific years
    (e.g. only-during-trending-regimes) but their cross-time mean is washed
    out by noise from other years. Bucketing IC by calendar year shows this.
    A factor with consistent IC sign across 5+ years is likely a real edge;
    one that flips sign year-to-year is regime-artefact.

(B) Multi-factor combination: when individual signals are weak (|IC| < 0.05)
    but uncorrelated, an IC-sign-weighted average can produce a meaningfully
    stronger combined signal — same logic as portfolio diversification, applied
    to alpha. v1 uses in-sample IC sign for weighting (acknowledged lookahead).
    A true backtest would rolling-weight; that's an M3 follow-up if v1 shows
    the combination at least directionally helps.

Output:
  research/factor_ic_by_year.csv  -- factor × year IC mean table
  research/factor_combined_ic.csv -- combined factor IC summary
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research.factor_eval import evaluate_factor, forward_return, rank_ic  # noqa: E402
from research.factors import FACTORS, cs_zscore  # noqa: E402
from research.panel_loader import load_panel  # noqa: E402

logger = logging.getLogger("m25")

# Single horizon for IC-by-year (5d = M2 sweet spot, balances noise vs trend).
YEAR_BUCKET_HORIZON = 5

# Minimum |IC mean| for a factor to be included in the combined blend. Very
# low threshold — we WANT to include weak signals; that's the point of blending.
COMBINE_THRESHOLD = 0.005


def compute_factor_series(panel: pd.DataFrame) -> dict[str, pd.Series]:
    """Compute every factor in the registry once, keep in memory."""
    series_map: dict[str, pd.Series] = {}
    for name, fn in FACTORS.items():
        try:
            series_map[name] = fn(panel)
        except Exception as e:
            logger.warning("factor %s failed to compute: %s", name, e)
    return series_map


def ic_by_year(
    factor_series_map: dict[str, pd.Series], panel: pd.DataFrame, horizon: int
) -> pd.DataFrame:
    """Per-factor IC bucketed by calendar year. Output: long-format DataFrame
    with columns [factor, year, ic_mean, ic_std, ic_ir, n_days]."""
    fwd = forward_return(panel, horizon)
    rows = []
    for name, factor in factor_series_map.items():
        ic = rank_ic(factor, fwd).dropna()
        if ic.empty:
            continue
        by_year = ic.groupby(ic.index.year).agg(["mean", "std", "count"])
        for year, stats in by_year.iterrows():
            mean_val = float(stats["mean"])
            std_val = float(stats["std"])
            rows.append(
                {
                    "factor": name,
                    "year": int(year),
                    "ic_mean": mean_val,
                    "ic_std": std_val,
                    "ic_ir": mean_val / std_val if std_val > 0 else float("nan"),
                    "n_days": int(stats["count"]),
                }
            )
    return pd.DataFrame(rows)


def stability_score(by_year_df: pd.DataFrame, min_years: int = 5) -> pd.DataFrame:
    """Per-factor stability: (a) IC sign consistency across years and (b) yearly
    IC mean of means. A factor with sign_consistency > 0.7 over 5+ years is
    a stronger edge candidate than M2's single-IR ranking suggests.

    Returns one row per factor, sorted by sign_consistency × |mean_yearly_ic|.
    """
    rows = []
    for factor, group in by_year_df.groupby("factor"):
        if len(group) < min_years:
            continue
        ics = group["ic_mean"].dropna()
        if len(ics) < min_years:
            continue
        # Sign consistency: fraction of years where IC has same sign as
        # the cross-year mean. 0.5 = random; 1.0 = always same sign.
        cross_mean = ics.mean()
        if cross_mean == 0:
            sign_consistency = 0.5
        else:
            sign_consistency = float((np.sign(ics) == np.sign(cross_mean)).mean())
        rows.append(
            {
                "factor": factor,
                "n_years": len(ics),
                "ic_mean_across_years": cross_mean,
                "ic_std_across_years": float(ics.std()),
                "sign_consistency": sign_consistency,
                "stability_score": sign_consistency * abs(cross_mean),
            }
        )
    return pd.DataFrame(rows).sort_values("stability_score", ascending=False)


def build_combined_factor(
    factor_series_map: dict[str, pd.Series], ic_signs: dict[str, int]
) -> pd.Series:
    """IC-sign-weighted z-score blend.

    For each included factor:
      contribution = cs_zscore(factor) × sign(IC_in_sample)
    Combined = mean across contributions per (date, symbol).

    Why mean instead of sum: keeps the combined factor on a comparable scale
    so IC magnitudes are interpretable. Equal-weight in z-space ≈ "average
    standardised opinion across factors."
    """
    pieces = []
    for name, factor in factor_series_map.items():
        sign = ic_signs.get(name, 0)
        if sign == 0:
            continue
        z = cs_zscore(factor)
        pieces.append((z * sign).rename(name))
    if not pieces:
        return pd.Series(dtype=float, name="combined")
    df = pd.concat(pieces, axis=1)
    return df.mean(axis=1, skipna=True).rename("combined")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("Loading panel...")
    panel = load_panel()
    n_sym = panel.index.get_level_values("symbol").nunique()
    print(f"Panel: {panel.shape[0]} rows × {n_sym} symbols")

    print(f"\nComputing {len(FACTORS)} factor series...")
    factor_series_map = compute_factor_series(panel)
    print(f"  {len(factor_series_map)} factors computed successfully")

    # ===== Part A: IC time-stability =====
    print(f"\n{'=' * 100}\nPart A: IC by year ({YEAR_BUCKET_HORIZON}d horizon)\n{'=' * 100}")
    by_year_df = ic_by_year(factor_series_map, panel, horizon=YEAR_BUCKET_HORIZON)
    out_year = REPO_ROOT / "research" / "factor_ic_by_year.csv"
    by_year_df.to_csv(out_year, index=False)
    print(f"Saved → {out_year}")

    # Pivoted year×factor view
    pivoted = (
        by_year_df.pivot(index="factor", columns="year", values="ic_mean").round(3).fillna(0.0)
    )
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print("\nIC mean by year (factor × year, 5d horizon):")
    print(pivoted.to_string())

    print(f"\n{'=' * 100}\nFactor stability ranking (≥5 years of data)\n{'=' * 100}")
    stab = stability_score(by_year_df)
    pd.set_option("display.float_format", lambda x: f"{x:.4f}")
    print(stab.to_string(index=False))

    # ===== Part B: Multi-factor combined =====
    print(
        f"\n{'=' * 100}\nPart B: Multi-factor combined (IC-sign weighted, {YEAR_BUCKET_HORIZON}d)\n{'=' * 100}"
    )
    fwd = forward_return(panel, YEAR_BUCKET_HORIZON)
    ic_signs: dict[str, int] = {}
    ic_means: dict[str, float] = {}
    for name, factor in factor_series_map.items():
        ic = rank_ic(factor, fwd).dropna()
        if ic.empty:
            continue
        mean = float(ic.mean())
        ic_means[name] = mean
        if abs(mean) > COMBINE_THRESHOLD:
            ic_signs[name] = int(np.sign(mean))

    print(
        f"\nFactors included (|IC mean| > {COMBINE_THRESHOLD}): "
        f"{len(ic_signs)} / {len(factor_series_map)}"
    )
    for name, sign in sorted(ic_signs.items(), key=lambda kv: -abs(ic_means[kv[0]])):
        print(f"  {name:<22s}  sign={sign:+d}  in-sample IC mean={ic_means[name]:+.4f}")

    if not ic_signs:
        print("No factors meet threshold. Combined factor would be empty.")
        return 0

    combined = build_combined_factor(factor_series_map, ic_signs)
    print(f"\nCombined factor: {combined.notna().sum()} non-NaN values")

    # Evaluate combined across horizons
    combined_res = evaluate_factor(combined, panel, horizons=(1, 5, 20))
    combined_res.insert(0, "factor", "COMBINED")
    print("\nCombined factor IC across horizons:")
    print(combined_res.to_string(index=False))

    out_combined = REPO_ROOT / "research" / "factor_combined_ic.csv"
    combined_res.to_csv(out_combined, index=False)
    print(f"\nSaved → {out_combined}")

    # ===== Verdict =====
    print(f"\n{'=' * 100}\nVERDICT\n{'=' * 100}")
    best_single = 0.108  # hl_range_20 @ 20d, from M2 v1
    best_combined_ir = float(combined_res["ic_ir"].abs().max())
    print(f"  Best single factor |IC IR| (M2 v1):     {best_single:.3f}")
    print(f"  Combined factor    |IC IR|:             {best_combined_ir:.3f}")
    delta_pct = (best_combined_ir - best_single) / best_single * 100
    print(f"  Δ = {best_combined_ir - best_single:+.3f} ({delta_pct:+.1f}%)")

    if best_combined_ir > 0.20:
        verdict = "[COMBINATION_HELPS_BIG] Combined IC IR > 0.20 — meaningful signal emerged."
    elif best_combined_ir > best_single * 1.3:
        verdict = "[COMBINATION_HELPS] Combined IC IR ≥ 30% above best single."
    elif best_combined_ir > best_single * 0.9:
        verdict = "[COMBINATION_NEUTRAL] Combined ~= best single. Factors are correlated."
    else:
        verdict = "[COMBINATION_HURTS] Combined < best single. Noise factors drag mean down."
    print(f"\n  {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
