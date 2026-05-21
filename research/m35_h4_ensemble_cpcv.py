"""M3.5: Recompute H4 ensemble Sharpe under Purged Walk-Forward (PWF).

M3 (research/m3_h4_cpcv.py) showed that the per-instrument OOS Sharpe of H4
members changes materially when WFA is replaced with PWF:
  AG  +0.424 (WFA) → +0.750 (PWF)   +77%
  I   +0.445 (WFA) → −0.316 (PWF)  −171%  (sign flip)
  CU  +0.568 (WFA) → +0.253 (PWF)   −55%

The open question (per project_research_layer2_status v13): does the H4
ensemble Sharpe of +0.993 (WFA, intersection period) hold up under PWF, or
is it inflated by the same train/test-window choice that helped I/CU under
WFA?

This script answers that. It mirrors h4_ensemble.py's per-instrument
inverse-train-vol scaling and date-intersection sum, but uses
cpcv.run_pwf(return_curves=True) instead of wfa.run_wfa(return_curves=True).
The ONLY difference is the outer fold loop — inner grid_search,
inverse-vol scaling, and portfolio construction are identical to h4.

Outputs (research/):
  - m35_h4_pwf_panel.csv          per-instrument scaled OOS daily PnL (intersection)
  - m35_h4_pwf_panel_union.csv    per-instrument scaled OOS daily PnL (union)
  - m35_h4_pwf_summary.csv        per-instrument + portfolio summary rows
  - m35_h4_pwf_yearly.csv         calendar-year portfolio Sharpe distribution

The "PWF distribution" of portfolio Sharpe is reported as per-calendar-year
sub-Sharpes (one Sharpe per year of the portfolio daily PnL series). This
is a downstream-of-portfolio view, not per-split (per-instrument splits
don't align in calendar time because each instrument's PWF runs over its
own date range), but it answers the same robustness question: is the
portfolio Sharpe consistent across regimes, or driven by a few good years?
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

# Identical to h4_ensemble.py — keep apples-to-apples.
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
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
MIN_TRADES = 5

# PWF settings — identical to research/m3_h4_cpcv.py for direct comparability.
PWF_N_FOLDS = 10
PWF_PURGE_DAYS = 20

# Target daily PnL std after inverse-vol scaling — identical to h4_ensemble.py.
TARGET_VOL = 5_000.0


def _daily_pnl_series(daily_df: pd.DataFrame | None) -> pd.Series:
    """Extract per-day net_pnl Series indexed by date. Mirror of h4_ensemble.py."""
    if daily_df is None or len(daily_df) == 0:
        return pd.Series(dtype=float)
    s = daily_df["net_pnl"].copy()
    s.index = pd.to_datetime(s.index)
    return s


def run_one_instrument(inst: dict) -> dict:
    """Run PWF with curve capture; return per-split inverse-vol-scaled OOS series.

    Each split's OOS net_pnl is scaled by TARGET_VOL / σ_train where σ_train is
    the std of the train-window daily net_pnl for the winning param set. Splits
    with σ_train == 0 (no trades in train) are skipped. PWF splits are
    non-overlapping by construction (folds are contiguous partitions), so the
    concat preserves the time axis.
    """
    from research.cpcv import run_pwf
    from strategies.double_ma_strategy import DoubleMaStrategy

    df, curves = run_pwf(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": inst.get("fixed_size", 1)},
        vt_symbol=inst["vt_symbol"],
        interval="1d",
        start=inst["start"],
        end=END_DATE,
        n_folds=PWF_N_FOLDS,
        purge_days=PWF_PURGE_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        return_curves=True,
        **inst["bt"],
    )

    scaled_pieces: list[pd.Series] = []
    split_diag: list[dict] = []
    for c in curves:
        is_pnl = _daily_pnl_series(c["is_daily_df"])
        oos_pnl = _daily_pnl_series(c["oos_daily_df"])
        if len(is_pnl) == 0 or len(oos_pnl) == 0:
            split_diag.append(
                {
                    "split_id": c["split_id"],
                    "sigma_train": None,
                    "weight": 0.0,
                    "skipped": True,
                }
            )
            continue
        sigma_train = float(is_pnl.std())
        if not np.isfinite(sigma_train) or sigma_train <= 0:
            split_diag.append(
                {
                    "split_id": c["split_id"],
                    "sigma_train": sigma_train,
                    "weight": 0.0,
                    "skipped": True,
                }
            )
            continue
        weight = TARGET_VOL / sigma_train
        scaled_pieces.append(oos_pnl * weight)
        split_diag.append(
            {
                "split_id": c["split_id"],
                "sigma_train": sigma_train,
                "weight": weight,
                "skipped": False,
            }
        )

    if not scaled_pieces:
        scaled = pd.Series(dtype=float)
    else:
        scaled = pd.concat(scaled_pieces).sort_index()
        # PWF splits are non-overlapping in test windows by partition, but
        # defensively coalesce if any date appears twice (e.g. boundary day).
        scaled = scaled.groupby(scaled.index).sum()

    return {"sym": inst["sym"], "pwf_df": df, "scaled_oos": scaled, "split_diag": split_diag}


def summarize_series(s: pd.Series, label: str) -> dict:
    """Daily Sharpe ×√252, max DD on cumulative PnL, total return. Mirror of h4."""
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


def yearly_sub_sharpes(portfolio: pd.Series) -> pd.DataFrame:
    """Sharpe per calendar year on the portfolio daily PnL series.

    Distribution view that complements the headline Sharpe number — answers
    'is the +X.XX Sharpe consistent across regimes, or driven by a few good
    years?'. Years with <60 trading days (typically incomplete calendar at
    series boundaries) are flagged but included.
    """
    if len(portfolio) == 0:
        return pd.DataFrame()
    by_year = portfolio.groupby(portfolio.index.year)
    rows = []
    for year, group in by_year:
        mean = float(group.mean())
        std = float(group.std())
        sharpe = (mean / std) * np.sqrt(252) if std > 0 else float("nan")
        rows.append(
            {
                "year": int(year),
                "days": int(len(group)),
                "sharpe": sharpe,
                "total_pnl": float(group.sum()),
                "incomplete": len(group) < 60,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("cpcv").setLevel(logging.WARNING)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(
        f"\n{'#' * 95}\n# M3.5 H4 ensemble under PWF: AG.adj15 + I.raw + CU.adj15"
        f"\n# n_folds={PWF_N_FOLDS}, purge={PWF_PURGE_DAYS}d, "
        f"grid={DM_GRID}, min_trades={MIN_TRADES}\n{'#' * 95}"
    )

    results = []
    for inst in INSTRUMENTS:
        print(f"\n--- {inst['sym']} ({inst['source']}) ---")
        r = run_one_instrument(inst)
        results.append(r)
        df = r["pwf_df"]
        oos = df["oos_sharpe"].dropna() if "oos_sharpe" in df.columns else pd.Series(dtype=float)
        n_used = sum(1 for d in r["split_diag"] if not d["skipped"])
        print(
            f"  PWF: {len(df)} splits, {n_used} weight-able, "
            f"OOS Sharpe mean={oos.mean():+.3f}, pos%={(oos > 0).mean() * 100:.1f}"
        )
        print(
            f"  scaled OOS series: {len(r['scaled_oos'])} days, "
            f"daily std={float(r['scaled_oos'].std()):.1f} (target {TARGET_VOL:.0f})"
        )

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
        f"  intersection range: "
        f"{intersection.index.min().date()} → {intersection.index.max().date()}"
    )

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

    print(f"\n{'=' * 80}\nPairwise daily-PnL correlations (intersection)\n{'=' * 80}")
    corr = intersection.corr()
    print(corr.round(3).to_string())

    portfolio = intersection.sum(axis=1)
    port_stats = summarize_series(portfolio, "PORTFOLIO")

    sum_sigma = sum(st["daily_std"] for st in per_inst_stats)
    port_sigma = port_stats["daily_std"]
    div_ratio = sum_sigma / port_sigma if port_sigma > 0 else float("nan")

    print(f"\n{'=' * 80}\nPORTFOLIO RESULT (PWF)\n{'=' * 80}")
    print(f"  days:                  {port_stats['days']}")
    print(f"  Sharpe (annualised):   {port_stats['sharpe_ann']:+.3f}")
    print(f"  daily mean:            {port_stats['daily_mean']:+.1f}")
    print(f"  daily std:             {port_stats['daily_std']:.1f}")
    print(f"  total PnL:             {port_stats['total_pnl']:+,.0f}")
    print(f"  max drawdown:          {port_stats['max_dd']:+,.0f}")
    print(f"  diversification ratio: {div_ratio:.3f}  (>1 = diversification benefit)")

    yearly = yearly_sub_sharpes(portfolio)
    if len(yearly):
        print(f"\n{'=' * 80}\nYearly portfolio sub-Sharpes (distribution view)\n{'=' * 80}")
        complete = yearly[~yearly["incomplete"]]
        print(f"  {len(yearly)} years total, {len(complete)} complete (>=60 trading days)")
        print("  Year-Sharpe distribution (complete years only):")
        if len(complete):
            s = complete["sharpe"]
            print(f"    mean={s.mean():+.3f}  median={s.median():+.3f}  std={s.std():.3f}")
            print(
                f"    min={s.min():+.3f}  max={s.max():+.3f}  positive %={float((s > 0).mean() * 100):.1f}"
            )
        print()
        for _, row in yearly.iterrows():
            tag = " (incomplete)" if row["incomplete"] else ""
            print(
                f"    {int(row['year'])}: Sharpe={row['sharpe']:+.3f}  "
                f"days={int(row['days'])}  pnl={row['total_pnl']:>+12,.0f}{tag}"
            )

    out_dir = REPO_ROOT / "research"
    intersection.to_csv(out_dir / "m35_h4_pwf_panel.csv")
    panel.to_csv(out_dir / "m35_h4_pwf_panel_union.csv")
    pd.DataFrame(per_inst_stats + [port_stats]).to_csv(
        out_dir / "m35_h4_pwf_summary.csv", index=False
    )
    if len(yearly):
        yearly.to_csv(out_dir / "m35_h4_pwf_yearly.csv", index=False)

    print(f"\n{'=' * 80}\nCOMPARISON vs H4 (WFA)\n{'=' * 80}")
    # Hard-coded H4 WFA reference numbers from project_research_layer2_status v6/v7.
    # These are intersection-period results (V0) so apples-to-apples with
    # this script's intersection summary, modulo the per-instrument start
    # difference between WFA folds and PWF folds (acknowledged caveat).
    wfa_ref = {"AG": 0.609, "I": 0.397, "CU": 0.723, "PORTFOLIO": 0.993}
    pwf_inst = {st["label"]: st["sharpe_ann"] for st in per_inst_stats}
    pwf_inst["PORTFOLIO"] = port_stats["sharpe_ann"]
    print(f"  {'inst':10s}  {'WFA':>7s}  {'PWF':>7s}  {'Δ':>7s}")
    for k in ("AG", "I", "CU", "PORTFOLIO"):
        wfa_v = wfa_ref[k]
        pwf_v = pwf_inst.get(k, float("nan"))
        delta = pwf_v - wfa_v
        print(f"  {k:10s}  {wfa_v:+7.3f}  {pwf_v:+7.3f}  {delta:+7.3f}")

    port_delta = port_stats["sharpe_ann"] - wfa_ref["PORTFOLIO"]
    print(f"\n{'=' * 80}\nVERDICT\n{'=' * 80}")
    if abs(port_delta) < 0.10:
        verdict = "[PWF_CONFIRMS] PWF portfolio Sharpe is within 0.10 of H4 WFA — robust."
    elif port_delta > 0:
        verdict = (
            "[PWF_RAISES] PWF portfolio Sharpe HIGHER than H4 WFA — the WFA "
            "evaluation may have been conservative."
        )
    elif port_delta > -0.30:
        verdict = (
            "[PWF_DEFLATES] PWF portfolio Sharpe materially below H4 WFA — H4 +0.993 "
            "is an upper bound; the central PWF estimate is lower but still positive."
        )
    else:
        verdict = (
            "[PWF_REJECTS] PWF portfolio Sharpe falls sharply — H4 +0.993 does NOT "
            "reproduce under purged walk-forward. Live promotion should not rely on the WFA number."
        )
    print(f"  Portfolio Δ (PWF - WFA): {port_delta:+.3f}")
    print(f"  → {verdict}")
    print(
        "\nArtefacts: m35_h4_pwf_panel.csv, m35_h4_pwf_summary.csv, "
        "m35_h4_pwf_yearly.csv → research/"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
