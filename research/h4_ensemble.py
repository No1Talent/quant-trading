"""H4 ensemble: equal-risk-weight AG+I+CU on best-per-instrument DoubleMa daily.

Background. H2 confirmed DoubleMa daily as a momentum FAMILY across AG, I, CU
(see project_research_layer2_status.md). Each instrument alone has OOS Sharpe
between +0.42 and +0.57, and IS-OOS Sharpe correlation around -0.20 — meaning
the per-instrument grid selector picks bad params almost as often as good ones.

Hypothesis: a portfolio of all three, equal-risk-weighted at fold boundaries
using inverse training-period vol, should:
  (a) reduce per-fold noise enough that the bad-pick problem becomes tolerable,
  (b) produce a portfolio OOS Sharpe higher than any single instrument,
  (c) reduce drawdown via the (presumed) low cross-instrument correlation.

Design.
  - Best-of-data per instrument (from H2 v5):
      AG → adj15 (Sharpe +0.424)
      I  → raw   (Sharpe +0.445)  -- adj15 broken under contango
      CU → adj15 (Sharpe +0.568)
  - Each instrument runs the same WFA (train=700d / test=250d / step=250d,
    DoubleMa grid fast∈{10,20,30} × slow∈{40,60,100}, min_trades=5).
  - For each fold, capture both IS and OOS daily PnL curves. Use IS vol
    (std of train-period strategy daily net_pnl) as the inverse-vol weight
    proxy — this is the only vol number known at fold-boundary without
    look-ahead.
  - Scaled OOS daily PnL = oos_pnl × (TARGET_VOL / σ_train).
  - Concatenate folds end-to-end per instrument.
  - Date intersection across all 3 instruments → portfolio is sum on common
    trading days only. Per [[project-research-layer2-status]] this is the
    cleanest measurement (other options: union+zero-fill, but inflates Sharpe
    during single-instrument early CU history).
  - Portfolio stats: Sharpe, max DD, total return, diversification ratio.

Output: per-instrument intersection-period Sharpe, pairwise daily-return
correlations, portfolio Sharpe vs the best single instrument.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]

import pandas as pd  # noqa: E402

INSTRUMENTS: list[dict[str, Any]] = [
    {
        "sym": "AG",
        "source": "adj15",
        "vt_symbol": "ag_continuous_adj15.SHFE",
        "start": datetime(2012, 5, 10),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=1, size=15, pricetick=1),
    },
    {
        "sym": "I",
        "source": "raw",
        "vt_symbol": "i_continuous.DCE",
        "start": datetime(2013, 10, 18),
        "bt": dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=100, pricetick=0.5),
    },
    {
        "sym": "CU",
        "source": "adj15",
        "vt_symbol": "cu_continuous_adj15.SHFE",
        "start": datetime(2005, 1, 4),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=10, size=5, pricetick=10),
    },
]

END_DATE = datetime(2026, 5, 15)
TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
MIN_TRADES = 5

# Target daily PnL std after inverse-vol scaling. Arbitrary positive number —
# Sharpe is invariant under constant rescaling. Pick something close to a
# realistic 1M-capital strategy's daily PnL std so reported numbers feel sane.
TARGET_VOL = 5_000.0  # ~0.5% of capital


def _daily_pnl_series(daily_df: pd.DataFrame | None) -> pd.Series:
    """Extract per-day net_pnl Series indexed by date.

    vn.py's BacktestingEngine.daily_df indexes by date (datetime.date) after
    calculate_result(). Guard for empty / missing.
    """
    if daily_df is None or len(daily_df) == 0:
        return pd.Series(dtype=float)
    s = daily_df["net_pnl"].copy()
    # Normalize index to pandas Timestamps for safe joins across instruments.
    s.index = pd.to_datetime(s.index)
    return s


def run_one_instrument(inst: dict) -> dict:
    """Run WFA with curve capture; return per-fold weighted OOS daily series.

    Each fold's OOS net_pnl is scaled by TARGET_VOL / σ_train, where σ_train is
    the std of the train-window daily net_pnl for the winning param set. Folds
    with σ_train == 0 (no trades in train) contribute zero (skipped).
    """
    from research.wfa import run_wfa
    from strategies.double_ma_strategy import DoubleMaStrategy

    df, curves = run_wfa(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": inst.get("fixed_size", 1)},
        vt_symbol=inst["vt_symbol"],
        interval="1d",
        start=inst["start"],
        end=END_DATE,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        return_curves=True,
        **inst["bt"],
    )

    scaled_pieces: list[pd.Series] = []
    fold_diag: list[dict] = []
    for c in curves:
        is_pnl = _daily_pnl_series(c["is_daily_df"])
        oos_pnl = _daily_pnl_series(c["oos_daily_df"])
        if len(is_pnl) == 0 or len(oos_pnl) == 0:
            fold_diag.append(
                {"fold": c["fold"], "sigma_train": None, "weight": 0.0, "skipped": True}
            )
            continue
        sigma_train = float(is_pnl.std())
        if not np.isfinite(sigma_train) or sigma_train <= 0:
            fold_diag.append(
                {"fold": c["fold"], "sigma_train": sigma_train, "weight": 0.0, "skipped": True}
            )
            continue
        weight = TARGET_VOL / sigma_train
        scaled_pieces.append(oos_pnl * weight)
        fold_diag.append(
            {"fold": c["fold"], "sigma_train": sigma_train, "weight": weight, "skipped": False}
        )

    if not scaled_pieces:
        scaled = pd.Series(dtype=float)
    else:
        # Concatenate. OOS folds are non-overlapping by construction
        # (step_days == test_days), so a plain concat preserves the time axis.
        scaled = pd.concat(scaled_pieces).sort_index()
        # Defensive: if any two folds share a date, sum them
        scaled = scaled.groupby(scaled.index).sum()

    return {"sym": inst["sym"], "wfa_df": df, "scaled_oos": scaled, "fold_diag": fold_diag}


def summarize_series(s: pd.Series, label: str) -> dict:
    """Daily Sharpe (×√252), max DD on cumulative PnL, total return."""
    if len(s) == 0:
        return {"label": label, "days": 0}
    mean = float(s.mean())
    std = float(s.std())
    sharpe = (mean / std) * np.sqrt(252) if std > 0 else float("nan")
    cum = s.cumsum()
    peak = cum.cummax()
    dd = cum - peak
    max_dd = float(dd.min())
    total = float(cum.iloc[-1])
    return {
        "label": label,
        "days": len(s),
        "sharpe_ann": sharpe,
        "daily_mean": mean,
        "daily_std": std,
        "total_pnl": total,
        "max_dd": max_dd,
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 90}\n# H4 ensemble: equal-risk-weight AG.adj15 + I.raw + CU.adj15\n{'#' * 90}")

    results = []
    for inst in INSTRUMENTS:
        print(f"\n--- {inst['sym']} ({inst['source']}) ---")
        r = run_one_instrument(inst)
        results.append(r)
        df = r["wfa_df"]
        oos = df["oos_sharpe"].dropna()
        n_used = sum(1 for d in r["fold_diag"] if not d["skipped"])
        print(
            f"  WFA: {len(df)} folds, {n_used} weight-able, "
            f"OOS Sharpe mean={oos.mean():+.3f}, pos%={(oos > 0).mean() * 100:.1f}"
        )
        print(
            f"  scaled OOS series: {len(r['scaled_oos'])} days, "
            f"daily std={float(r['scaled_oos'].std()):.1f} (target {TARGET_VOL:.0f})"
        )

    # Build per-instrument scaled-PnL DataFrame, align on date intersection
    panel = pd.DataFrame({r["sym"]: r["scaled_oos"] for r in results})
    intersection = panel.dropna(how="any")
    print(f"\n{'=' * 80}\nDate-intersection panel\n{'=' * 80}")
    print(
        f"  union days: {len(panel)}, intersection days: {len(intersection)} "
        f"(drop ratio: {1 - len(intersection) / max(len(panel), 1):.1%})"
    )
    if len(intersection) == 0:
        print("  ERROR: no overlap across all 3 instruments. Check date ranges.")
        return 1
    print(
        f"  intersection range: {intersection.index.min().date()} → {intersection.index.max().date()}"
    )

    # Per-instrument stats on intersection only — apples-to-apples vs portfolio
    print(f"\n{'=' * 80}\nPer-instrument stats (intersection period)\n{'=' * 80}")
    per_inst_stats = []
    for col in intersection.columns:
        st = summarize_series(intersection[col], col)
        per_inst_stats.append(st)
        print(
            f"  {col:3s}: Sharpe={st['sharpe_ann']:+.3f}  "
            f"total={st['total_pnl']:>12,.0f}  maxDD={st['max_dd']:>+12,.0f}  "
            f"σ_daily={st['daily_std']:.1f}"
        )

    # Pairwise daily-return correlations
    print(f"\n{'=' * 80}\nPairwise daily-PnL correlations (intersection)\n{'=' * 80}")
    corr = intersection.corr()
    print(corr.round(3).to_string())

    # Portfolio: equal-weight on scaled (= equal-risk) → simple sum
    portfolio = intersection.sum(axis=1)
    port_stats = summarize_series(portfolio, "PORTFOLIO")

    # Diversification ratio Σ(σ_i) / σ_port (weights are equal in scaled space)
    sum_sigma = sum(st["daily_std"] for st in per_inst_stats)
    port_sigma = port_stats["daily_std"]
    div_ratio = sum_sigma / port_sigma if port_sigma > 0 else float("nan")

    print(f"\n{'=' * 80}\nPORTFOLIO RESULT\n{'=' * 80}")
    print(f"  days:                  {port_stats['days']}")
    print(f"  Sharpe (annualised):   {port_stats['sharpe_ann']:+.3f}")
    print(f"  daily mean:            {port_stats['daily_mean']:+.1f}")
    print(f"  daily std:             {port_stats['daily_std']:.1f}")
    print(f"  total PnL:             {port_stats['total_pnl']:+,.0f}")
    print(f"  max drawdown:          {port_stats['max_dd']:+,.0f}")
    print(f"  diversification ratio: {div_ratio:.3f}  (>1 = diversification benefit)")

    # Verdict
    best_single_sharpe = max(st["sharpe_ann"] for st in per_inst_stats)
    best_single_name = max(per_inst_stats, key=lambda s: s["sharpe_ann"])["label"]
    # Persist artefacts BEFORE final print so they survive any encoding issues.
    out_dir = REPO_ROOT / "research"
    intersection.to_csv(out_dir / "h4_ensemble_daily_panel.csv")
    panel.to_csv(out_dir / "h4_ensemble_daily_panel_union.csv")
    pd.DataFrame(per_inst_stats + [port_stats]).to_csv(
        out_dir / "h4_ensemble_summary.csv", index=False
    )

    print(f"\n{'=' * 80}\nVERDICT\n{'=' * 80}")
    print(
        f"  Best single instrument (intersection):  {best_single_name} Sharpe={best_single_sharpe:+.3f}"
    )
    print(f"  Portfolio Sharpe:                       {port_stats['sharpe_ann']:+.3f}")
    delta = port_stats["sharpe_ann"] - best_single_sharpe
    if delta > 0.10:
        verdict = "[ENSEMBLE_HELPS] Portfolio Sharpe meaningfully > best single."
    elif delta > -0.05:
        verdict = "[ENSEMBLE_NEUTRAL] Portfolio Sharpe ~ best single -- diversification offsets nothing extra."
    else:
        verdict = (
            "[ENSEMBLE_HURTS] Portfolio Sharpe materially < best single -- weaker members drag."
        )
    print(f"  Delta Sharpe (portfolio - best single): {delta:+.3f}")
    print(f"  -> {verdict}")
    print("\nArtefacts: h4_ensemble_daily_panel.csv, h4_ensemble_summary.csv -> research/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
