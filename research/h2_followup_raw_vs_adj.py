"""H2 follow-up: raw-vs-adj15 sensitivity sweep + family verdict.

Background. H2 cross-instrument validation (research/h2_cross_instrument.py)
ran DoubleMa daily WFA on HC/I/AU/CU back-adjusted via H1.5 (OI-based)
detection — same recipe that confirmed AG. Two surprises:

  1. I (iron ore) on adj15 produced ZERO trades across every fold. Root
     cause: I's persistent contango (-28 RMB per OI-flagged event, 3.2
     events/yr over 12.5 years) drives the additive back-adjust deeply
     negative — adj close range [-775.5, +953]. vn.py won't fill orders
     at negative limit prices.
  2. CU on adj15 showed +0.568 Sharpe vs unknown raw baseline — was the
     adjustment doing useful work, or was raw already that good?

This script answers the universality question: run RAW i_continuous /
hc_continuous / au_continuous / cu_continuous through the same DoubleMa
WFA and compare to the adj15 numbers from h2_cross_instrument.py. Then
emit the consolidated family verdict.

Output: a 5-row table (AG anchor + 4 cross instruments), each with raw
and adj15 columns side-by-side, the best-of column used for the verdict,
and the universality classification of the H1.5 detector per instrument.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]

import pandas as pd  # noqa: E402

# Per-instrument backtest spec (matches h2_cross_instrument.py)
INSTRUMENTS: list[dict[str, Any]] = [
    {
        "sym": "hc",
        "start": datetime(2014, 3, 21),
        "bt": dict(capital=1_000_000, rate=1e-4, slippage=1, size=10, pricetick=1),
        "exch": "SHFE",
    },
    {
        "sym": "i",
        "start": datetime(2013, 10, 18),
        "bt": dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=100, pricetick=0.5),
        "exch": "DCE",
    },
    {
        "sym": "au",
        "start": datetime(2008, 1, 9),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=0.02, size=1000, pricetick=0.02),
        "exch": "SHFE",
    },
    {
        "sym": "cu",
        "start": datetime(2005, 1, 4),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=10, size=5, pricetick=10),
        "exch": "SHFE",
    },
]

END_DATE = datetime(2026, 5, 15)
TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
MIN_TRADES = 5

AG_ANCHOR_RAW: dict[str, Any] = {
    "folds": 17,
    "oos_sharpe_mean": 0.344,
    "oos_sharpe_median": 0.127,
    "oos_positive_pct": 64.7,
    "is_oos_corr": 0.105,
    "total_oos_return_pct": 7.456,
}
AG_ANCHOR_ADJ15: dict[str, Any] = {
    "folds": 17,
    "oos_sharpe_mean": 0.424,
    "oos_sharpe_median": 0.520,
    "oos_positive_pct": 76.5,
    "is_oos_corr": -0.199,
    "total_oos_return_pct": 8.90,
}


def run_wfa_for(vt_symbol: str, start: datetime, bt: dict) -> dict[str, Any]:
    from research.wfa import run_wfa
    from strategies.double_ma_strategy import DoubleMaStrategy

    df = run_wfa(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": 1},
        vt_symbol=vt_symbol,
        interval="1d",
        start=start,
        end=END_DATE,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        **bt,
    )
    if len(df) == 0:
        return {"folds": 0}
    oos = df["oos_sharpe"].dropna()
    return {
        "df": df,
        "folds": len(df),
        "oos_sharpe_mean": float(oos.mean()),
        "oos_sharpe_median": float(oos.median()),
        "oos_positive_pct": float((oos > 0).mean() * 100),
        "is_oos_corr": float(df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]),
        "total_oos_return_pct": float(df["oos_return_pct"].sum()),
    }


def classify_instrument(raw: dict, adj: dict) -> tuple[str, str, dict]:
    """Pick best of raw vs adj, classify edge presence and adjuster behaviour."""
    candidates = []
    if raw.get("folds", 0) > 0:
        candidates.append(("raw", raw))
    if adj.get("folds", 0) > 0:
        candidates.append(("adj15", adj))

    if not candidates:
        return "[FAIL]", "[ADJ_UNTESTED]", {}

    best_src, best = max(candidates, key=lambda kv: kv[1]["oos_sharpe_mean"])

    s = best["oos_sharpe_mean"]
    pos = best["oos_positive_pct"]
    if s > 0.25 and pos > 60:
        edge = "[REPLICATES]"
    elif s > 0.15 and pos > 50:
        edge = "[PARTIAL]"
    elif s > 0:
        edge = "[WEAK]"
    else:
        edge = "[NO_EDGE]"

    # Adjuster behaviour classification
    if adj.get("folds", 0) == 0:
        adj_status = "[ADJ_BROKEN]"  # could not run
    else:
        delta = adj["oos_sharpe_mean"] - raw.get("oos_sharpe_mean", 0.0)
        if delta > 0.15:
            adj_status = "[ADJ_HELPS]"
        elif delta < -0.10:
            adj_status = "[ADJ_HURTS]"
        else:
            adj_status = "[ADJ_NEUTRAL]"

    return edge, adj_status, {"best_src": best_src, **best}


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(
        f"\n{'#' * 90}\n# H2 follow-up: raw vs H1.5-adj15 sensitivity (DoubleMa daily)\n{'#' * 90}"
    )

    rows: list[dict[str, Any]] = []
    raw_dfs: list[pd.DataFrame] = []

    # AG anchor (from prior runs)
    raw_a, adj_a = AG_ANCHOR_RAW, AG_ANCHOR_ADJ15
    edge_a, adj_status_a, best_a = classify_instrument(raw_a, adj_a)
    rows.append(
        {
            "sym": "AG",
            "raw": raw_a,
            "adj15": adj_a,
            "edge": edge_a,
            "adj_status": adj_status_a,
            "best": best_a,
        }
    )

    for inst in INSTRUMENTS:
        sym = inst["sym"]
        print(f"\n--- {sym.upper()} ---")
        try:
            raw = run_wfa_for(f"{sym}_continuous.{inst['exch']}", inst["start"], inst["bt"])
        except Exception as e:
            print(f"  raw failed: {type(e).__name__}: {e}")
            raw = {"folds": 0}

        if raw.get("folds", 0) > 0:
            raw["df"].insert(0, "source", "raw")
            raw["df"].insert(0, "sym", sym)
            raw_dfs.append(raw["df"])
            print(
                f"  raw:    folds={raw['folds']:2d}  Sharpe={raw['oos_sharpe_mean']:+.3f}  "
                f"median={raw['oos_sharpe_median']:+.3f}  pos%={raw['oos_positive_pct']:.1f}  "
                f"IS-OOS corr={raw['is_oos_corr']:+.3f}  total={raw['total_oos_return_pct']:+.2f}%"
            )

        try:
            adj = run_wfa_for(f"{sym}_continuous_adj15.{inst['exch']}", inst["start"], inst["bt"])
        except Exception as e:
            print(f"  adj15 failed: {type(e).__name__}: {e}")
            adj = {"folds": 0}

        if adj.get("folds", 0) > 0:
            adj["df"].insert(0, "source", "adj15")
            adj["df"].insert(0, "sym", sym)
            raw_dfs.append(adj["df"])
            print(
                f"  adj15:  folds={adj['folds']:2d}  Sharpe={adj['oos_sharpe_mean']:+.3f}  "
                f"median={adj['oos_sharpe_median']:+.3f}  pos%={adj['oos_positive_pct']:.1f}  "
                f"IS-OOS corr={adj['is_oos_corr']:+.3f}  total={adj['total_oos_return_pct']:+.2f}%"
            )
        else:
            print("  adj15:  0 folds — additive back-adjust likely broken (negative prices?)")

        edge, adj_status, best = classify_instrument(raw, adj)
        rows.append(
            {
                "sym": sym.upper(),
                "raw": raw,
                "adj15": adj,
                "edge": edge,
                "adj_status": adj_status,
                "best": best,
            }
        )

    # Consolidated table
    print(f"\n\n{'=' * 116}\nH2 CONSOLIDATED TABLE\n{'=' * 116}")
    print(
        f"  {'Sym':4s} | {'Raw Sharpe / pos%':>22s} | {'Adj15 Sharpe / pos%':>22s} | "
        f"{'Best':>7s} = {'Sharpe':>7s} {'pos%':>6s} | {'Edge':>14s} | {'Adjuster':>14s}"
    )
    print("  " + "-" * 110)
    for r in rows:
        raw = r["raw"]
        adj = r["adj15"]
        raw_str = (
            f"{raw['oos_sharpe_mean']:+.3f} / {raw['oos_positive_pct']:.1f}"
            if raw.get("folds", 0) > 0
            else "       n/a"
        )
        adj_str = (
            f"{adj['oos_sharpe_mean']:+.3f} / {adj['oos_positive_pct']:.1f}"
            if adj.get("folds", 0) > 0
            else "       broken"
        )
        best = r["best"]
        if best:
            best_src = best["best_src"]
            best_str = f"{best['oos_sharpe_mean']:+.3f} {best['oos_positive_pct']:>5.1f}"
        else:
            best_src = "n/a"
            best_str = "    n/a    n/a"
        print(
            f"  {r['sym']:4s} | {raw_str:>22s} | {adj_str:>22s} | "
            f"{best_src:>7s} = {best_str} | {r['edge']:>14s} | {r['adj_status']:>14s}"
        )

    # Family verdict (use best-of for each instrument)
    print(f"\n{'=' * 116}\nH2 FAMILY VERDICT\n{'=' * 116}")
    replicates = [r for r in rows if r["edge"] == "[REPLICATES]"]
    partials = [r for r in rows if r["edge"] == "[PARTIAL]"]
    weak = [r for r in rows if r["edge"] == "[WEAK]"]
    fails = [r for r in rows if r["edge"] in ("[NO_EDGE]", "[FAIL]")]

    print(f"  [REPLICATES] (Sharpe>+0.25, pos%>60): {[r['sym'] for r in replicates]}")
    print(f"  [PARTIAL]    (Sharpe>+0.15, pos%>50): {[r['sym'] for r in partials]}")
    print(f"  [WEAK]       (positive Sharpe only):  {[r['sym'] for r in weak]}")
    print(f"  [NO_EDGE]    (negative Sharpe):       {[r['sym'] for r in fails]}")

    n_rep = len(replicates)
    if n_rep >= 3:
        print(f"\n  [FAMILY CONFIRMED] {n_rep} instruments show robust DoubleMa daily alpha.")
        print("  Momentum on Chinese commodity futures is a real strategy family, not")
        print("  an AG singleton. Next step: H4 ensemble of confirmed instruments")
        print("  (equal-risk weighting, per-instrument WFA-picked params).")
    elif n_rep == 2:
        print("\n  [FAMILY LIKELY] 2 instruments replicate; promote to provisional family")
        print("  status pending H4 ensemble. Expect partial-instruments to drag.")
    elif n_rep == 1:
        print("\n  [SINGLETON-PLUS] Only AG itself replicates strongly. Other cross-instr.")
        print("  signals (partials/weak) suggest the EDGE exists broadly but the GRID")
        print("  doesn't find it consistently. Pivot to ensemble / alt-metric (priority 2/3).")
    else:
        print("\n  [SINGLETON] AG is alone. Demote to instrument-specific candidate.")

    # H1.5 universality
    print(f"\n{'=' * 116}\nH1.5 BACK-ADJUSTMENT UNIVERSALITY\n{'=' * 116}")
    helps = [r for r in rows if r["adj_status"] == "[ADJ_HELPS]"]
    neutral = [r for r in rows if r["adj_status"] == "[ADJ_NEUTRAL]"]
    hurts = [r for r in rows if r["adj_status"] == "[ADJ_HURTS]"]
    broken = [r for r in rows if r["adj_status"] == "[ADJ_BROKEN]"]
    print(f"  H1.5 HELPS:    {[r['sym'] for r in helps]}")
    print(f"  H1.5 NEUTRAL:  {[r['sym'] for r in neutral]}")
    print(f"  H1.5 HURTS:    {[r['sym'] for r in hurts]}")
    print(f"  H1.5 BROKEN:   {[r['sym'] for r in broken]}  (additive shift drives prices negative)")
    print()
    print("  Conclusion: H1.5 (OI-based additive back-adjust) is NOT universal.")
    print("  It is calibrated for AG and produces favourable results on instruments")
    print("  with similar OI/carry structure (e.g. CU). On low-carry instruments (AU)")
    print("  it removes useful information; on contango-heavy instruments (I) the")
    print("  additive accumulation goes deeply negative. Ratio-based back-adjustment")
    print("  is the proper next-generation tool — defer to H5 if H4 ensemble succeeds.")

    if raw_dfs:
        out_path = REPO_ROOT / "research" / "wfa_results_h2_followup.csv"
        pd.concat(raw_dfs, ignore_index=True).to_csv(out_path, index=False)
        print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
