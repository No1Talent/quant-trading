"""BollReversal on RB 60min — 8-contract pressure test of the +0.12 OOS / -0.73 corr.

Original BollRev/RB (4 contracts, 8 folds) showed the most curious result of
the entire phase: OOS Sharpe mean +0.12 with 62% positive folds (best raw OOS
across all 6 strategy x instrument combos), but IS-OOS corr -0.73 — strong
'has alpha but optimizer picks the wrong params' signature.

Question: does the OOS-positive pattern survive when we double the sample to
8 contracts (rb2210, rb2301, rb2305 = 2022 dump; rb2310, rb2401, rb2405,
rb2410 = 2023-2024 base; rb2501 = late 2024-early 2025)? Does the -0.73
correlation persist, or was that also a 4-contract artifact?

Control variables (identical to original 4-contract run):
  param grid: boll_window [20,30,40] x boll_dev [2.0, 2.5, 3.0]
  windows: train=120, test=60, step=60 calendar days
  min_trades=10, bt_kwargs matches RB (size=10, rate=1e-4)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from research.wfa_rb_batch import BT_KWARGS as RB_BT_KWARGS  # noqa: E402
from research.wfa_rb_batch import CONTRACTS as RB_CONTRACTS  # noqa: E402
from research.wfa_rb_batch import run_batch  # noqa: E402

PARAM_GRID: dict[str, list[Any]] = {
    "boll_window": [20, 30, 40],
    "boll_dev": [2.0, 2.5, 3.0],
}

# Baseline from prior 4-contract run for direct comparison
BASELINE_4_CONTRACT = {
    "folds": 8,
    "oos_sharpe_mean": 0.116,
    "oos_sharpe_median": 0.412,
    "oos_positive_pct": 62.5,
    "is_sharpe_mean": 1.087,
    "is_oos_corr": -0.725,
    "total_oos_return_pct": -0.077,
}


def main() -> int:
    from strategies.boll_reversal_strategy import BollReversalStrategy

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 80}\n# BollReversal on RB (8 contracts) — deep dive\n{'#' * 80}")
    print(f"  Contracts: {[c[0] for c in RB_CONTRACTS]}")

    df = run_batch(
        strategy_class=BollReversalStrategy,
        param_grid=PARAM_GRID,
        fixed_params={"fixed_size": 1},
        label="BollRev",
        contracts=RB_CONTRACTS,
        train_days=120,
        test_days=60,
        step_days=60,
        bt_kwargs=RB_BT_KWARGS,
        interval="1h",
        min_trades=10,
    )

    if df.empty:
        print("Batch produced no folds.")
        return 1

    pd.set_option("display.width", 230)
    pd.set_option("display.max_columns", 30)

    print(f"\n{'=' * 110}\nALL FOLDS\n{'=' * 110}")
    print(df.to_string(index=False))

    oos = df["oos_sharpe"].dropna()
    is_ = df["is_sharpe"].dropna()
    corr = df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]
    pos_pct = (oos > 0).mean() * 100

    print(f"\n{'=' * 110}\n8-CONTRACT vs 4-CONTRACT BASELINE\n{'=' * 110}")
    rows = [
        ("Folds", len(df), BASELINE_4_CONTRACT["folds"]),
        ("OOS Sharpe mean", oos.mean(), BASELINE_4_CONTRACT["oos_sharpe_mean"]),
        ("OOS Sharpe median", oos.median(), BASELINE_4_CONTRACT["oos_sharpe_median"]),
        ("OOS positive %", pos_pct, BASELINE_4_CONTRACT["oos_positive_pct"]),
        ("IS Sharpe mean", is_.mean(), BASELINE_4_CONTRACT["is_sharpe_mean"]),
        ("IS-OOS corr", corr, BASELINE_4_CONTRACT["is_oos_corr"]),
        (
            "Total OOS return %",
            df["oos_return_pct"].sum(),
            BASELINE_4_CONTRACT["total_oos_return_pct"],
        ),
    ]
    print(f"  {'Metric':22s} {'8-contract':>14s} {'4-contract':>14s} {'Δ':>12s}")
    print("  " + "-" * 66)
    for name, v8, v4 in rows:
        delta = v8 - v4
        print(f"  {name:22s} {v8:>+14.3f} {v4:>+14.3f} {delta:>+12.3f}")

    print(f"\n{'=' * 110}\nPER-CONTRACT OOS BREAKDOWN\n{'=' * 110}")
    per_ct = (
        df.groupby("contract")
        .agg(
            folds=("fold", "count"),
            oos_sharpe_mean=("oos_sharpe", "mean"),
            oos_sharpe_min=("oos_sharpe", "min"),
            oos_sharpe_max=("oos_sharpe", "max"),
            oos_return_pct_sum=("oos_return_pct", "sum"),
            oos_positive=("oos_sharpe", lambda s: (s > 0).sum()),
        )
        .round(3)
    )
    print(per_ct.to_string())

    print(f"\n{'=' * 110}\nIS-PICKED PARAMS\n{'=' * 110}")
    for p, n in df["best_params"].astype(str).value_counts().items():
        print(f"  {p}: {n} fold(s)")

    print(f"\n{'=' * 110}\nVERDICT\n{'=' * 110}")
    # BollRev/RB's distinguishing feature was OOS positive % at 62 with negative corr.
    # Use both axes to assess.
    if pos_pct >= 60 and oos.mean() > 0.1:
        if corr < -0.3:
            print("  [PARADOX HELD] OOS still positive AND IS-OOS corr still negative.")
            print("                 The 'has alpha, optimizer picks wrong' pattern is real.")
            print("                 Next: try parameter ensemble or non-Sharpe selection.")
        else:
            print("  [EDGE+SELECTABLE] OOS held positive AND corr moved up. The signal")
            print("                    is real and can be captured. Move toward deployment study.")
    elif pos_pct >= 50 and oos.mean() > 0:
        print("  [WEAKER] OOS positivity softened but still > 50% with positive mean.")
        print("           Marginal edge — worth ensemble/regime study.")
    elif oos.mean() > 0:
        print("  [DEGRADED] OOS dropped below 4-contract result but still positive average.")
        print("             4-contract was partly noise; real signal smaller than thought.")
    else:
        print("  [GONE] OOS mean turned negative. The 62%/+0.12 was a 4-contract")
        print("         artifact. BollRev/RB joins the 'no consistent alpha' bucket.")

    out_path = REPO_ROOT / "research" / "wfa_results_rb_boll_deepdive.csv"
    df.to_csv(out_path, index=False)
    print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
