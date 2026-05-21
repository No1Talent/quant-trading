"""Cross-sectional IC evaluator for the factor library.

For each (factor × forward_horizon), compute:
  - IC (per-date Spearman rank correlation between factor at t and ret_{t+k})
  - IC IR (mean / std of IC over time — higher = more consistent signal)
  - IC positive %
  - Quintile spread: mean(top quintile fwd ret) - mean(bottom quintile fwd ret)
  - Day count

Why minimal-IC instead of alphalens-reloaded for v1:
  - Faster to ship; no new dep on statsmodels/matplotlib bundle.
  - Pinpoints which factors have signal at all — that's the first question.
  - Tear-sheet visualisation (decay, turnover, plots) becomes useful only
    AFTER ≥1 factor has IC > 0.03. If nothing has signal, alphalens is
    polish on noise.

Conventions match research.factors:
  - factor: pd.Series indexed by MultiIndex(datetime, symbol)
  - forward return: same shape, computed as log(close_{t+n} / close_t) so
    units match factor return-like factors (additive across horizons).

The IC sign is direction-agnostic at this stage. A factor with mean IC = -0.04
is just as good as +0.04 — you'd just trade the opposite leg. We sort the
final table by |IC IR| descending so winners surface regardless of sign.
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

from research.factors import FACTORS  # noqa: E402
from research.panel_loader import load_panel  # noqa: E402

logger = logging.getLogger("factor_eval")


def forward_return(panel: pd.DataFrame, n: int) -> pd.Series:
    """Forward log return: log(close_{t+n} / close_t), indexed at t.

    Long-format Series with MultiIndex(datetime, symbol). NaN for the last n
    rows (no future data) and for any symbol's pre-listing dates.
    """
    close = panel["close"].unstack("symbol").sort_index()
    fwd_wide = np.log(close.shift(-n) / close)
    s = fwd_wide.stack(future_stack=True)
    s.index.names = ["datetime", "symbol"]
    s.name = f"fwd_ret_{n}"
    return s


def rank_ic(factor: pd.Series, fwd_ret: pd.Series, min_symbols: int = 5) -> pd.Series:
    """Per-date Spearman rank IC between factor and forward return.

    `min_symbols` filters out days with too few symbols (early history when
    most contracts hadn't listed yet) — those days' IC is rank-2 corr, pure
    noise. 5 is a soft floor; with 20 symbols typical we have 15-20 daily.
    """
    df = pd.DataFrame({"factor": factor, "fwd": fwd_ret}).dropna()
    if df.empty:
        return pd.Series(dtype=float)

    ics: dict[pd.Timestamp, float] = {}
    for date, group in df.groupby(level="datetime"):
        if len(group) < min_symbols:
            continue
        ics[date] = group["factor"].corr(group["fwd"], method="spearman")
    return pd.Series(ics).sort_index()


def quintile_spread(
    factor: pd.Series, fwd_ret: pd.Series, n_q: int = 5, min_symbols: int = 5
) -> float:
    """Mean (top quintile fwd ret) - (bottom quintile fwd ret), in log-return
    units. Strong signal if |spread| > 0.001 (10 bps per period).

    Per-date: bucket symbols by factor into n_q quantiles, take fwd_ret mean
    in each bucket. Aggregate across dates (simple mean of daily spreads).
    Falls back to NaN if too few symbols to form n_q buckets.

    Filters ±inf in fwd_ret (artefact of close==0 in vn.py DB on rare days)
    by treating as NaN; otherwise one bad bar pollutes the whole mean.
    """
    df = (
        pd.DataFrame({"factor": factor, "fwd": fwd_ret}).replace([np.inf, -np.inf], np.nan).dropna()
    )
    if df.empty:
        return float("nan")

    spreads: list[float] = []
    for _, g in df.groupby(level="datetime"):
        if len(g) < max(min_symbols, n_q):
            continue
        try:
            q = pd.qcut(g["factor"], n_q, labels=False, duplicates="drop")
        except ValueError:
            continue
        # `labels=False` → integer bins 0..(n_q-1). 0 = lowest factor, n_q-1 = highest.
        top_mask = q == (n_q - 1)
        bot_mask = q == 0
        if not top_mask.any() or not bot_mask.any():
            continue
        spreads.append(g.loc[top_mask, "fwd"].mean() - g.loc[bot_mask, "fwd"].mean())
    return float(np.mean(spreads)) if spreads else float("nan")


def evaluate_factor(
    factor: pd.Series, panel: pd.DataFrame, horizons: tuple[int, ...] = (1, 5, 20)
) -> pd.DataFrame:
    """IC summary for one factor across forward horizons. One row per horizon."""
    rows = []
    for h in horizons:
        fwd = forward_return(panel, h)
        ic = rank_ic(factor, fwd)
        ic_clean = ic.dropna()

        if ic_clean.empty:
            rows.append(
                {
                    "horizon": h,
                    "ic_mean": np.nan,
                    "ic_std": np.nan,
                    "ic_ir": np.nan,
                    "ic_positive_pct": np.nan,
                    "n_days": 0,
                    "q1_q5_spread": np.nan,
                }
            )
            continue

        ic_mean = float(ic_clean.mean())
        ic_std = float(ic_clean.std())
        rows.append(
            {
                "horizon": h,
                "ic_mean": ic_mean,
                "ic_std": ic_std,
                "ic_ir": ic_mean / ic_std if ic_std > 0 else np.nan,
                "ic_positive_pct": float((ic_clean > 0).mean() * 100),
                "n_days": int(len(ic_clean)),
                "q1_q5_spread": quintile_spread(factor, fwd),
            }
        )

    return pd.DataFrame(rows)


def evaluate_all(panel: pd.DataFrame, horizons: tuple[int, ...] = (1, 5, 20)) -> pd.DataFrame:
    """Run every FACTORS entry through evaluate_factor, return long-format table."""
    all_rows = []
    for name, fn in FACTORS.items():
        try:
            factor = fn(panel)
        except Exception as e:
            logger.warning("factor %s failed to compute: %s", name, e)
            continue
        try:
            res = evaluate_factor(factor, panel, horizons)
        except Exception as e:
            logger.warning("factor %s evaluation failed: %s", name, e)
            continue
        res.insert(0, "factor", name)
        all_rows.append(res)
        logger.info(
            "  %-22s  IC@1=%+.4f  IC@5=%+.4f  IC@20=%+.4f",
            name,
            res.loc[res["horizon"] == 1, "ic_mean"].iloc[0] if 1 in horizons else np.nan,
            res.loc[res["horizon"] == 5, "ic_mean"].iloc[0] if 5 in horizons else np.nan,
            res.loc[res["horizon"] == 20, "ic_mean"].iloc[0] if 20 in horizons else np.nan,
        )

    if not all_rows:
        return pd.DataFrame()
    return pd.concat(all_rows, ignore_index=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("Loading panel from cache (or rebuilding if stale)...")
    panel = load_panel()
    n_sym = panel.index.get_level_values("symbol").nunique()
    print(
        f"Panel: {panel.shape[0]} rows × {n_sym} symbols, "
        f"{panel.index.get_level_values('datetime').min().date()} → "
        f"{panel.index.get_level_values('datetime').max().date()}"
    )

    print(f"\nEvaluating {len(FACTORS)} factors × 3 horizons ({1}, {5}, {20})...")
    df = evaluate_all(panel)

    if df.empty:
        print("No factors successfully evaluated.")
        return 1

    # Sort by |IC IR| descending so winners surface regardless of sign.
    df_sorted = (
        df.assign(_abs_ir=df["ic_ir"].abs())
        .sort_values("_abs_ir", ascending=False, na_position="last")
        .drop(columns="_abs_ir")
        .reset_index(drop=True)
    )

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 12)
    pd.set_option("display.float_format", lambda x: f"{x:.4f}")
    print(f"\n{'=' * 110}\nIC SUMMARY (sorted by |IC IR| desc)\n{'=' * 110}")
    print(df_sorted.to_string(index=False))

    # Highlight signal candidates: |IC IR| > 0.3 is a soft "this might be real"
    # threshold for daily cross-section. Mature equity factors hit 0.5-1.0;
    # in commodity futures with N=20 we expect lower.
    signal_mask = df_sorted["ic_ir"].abs() > 0.3
    print(f"\n{'=' * 110}\nCANDIDATES (|IC IR| > 0.3)\n{'=' * 110}")
    if signal_mask.any():
        print(df_sorted[signal_mask].to_string(index=False))
    else:
        max_ir = df_sorted["ic_ir"].abs().max()
        print(f"  None. Highest |IC IR| = {max_ir:.3f}.")
        print("  Diagnosis options:")
        print(
            "   - N=20 symbols × ~2700 intersection days may be too few for "
            "stable IC. Add more symbols (M0.5 retry)."
        )
        print("   - Forward horizon mismatch: try 60d/120d for slow factors.")
        print("   - Cross-section may need sector neutralisation (industry rank).")

    out = REPO_ROOT / "research" / "factor_ic_summary.csv"
    df_sorted.to_csv(out, index=False)
    print(f"\nSaved → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
