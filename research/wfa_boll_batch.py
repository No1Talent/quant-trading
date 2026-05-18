"""WFA on BollReversal (mean-reversion) across rb + ag 60min.

Decisive test: if mean-reversion shows positive IS-OOS correlation where
momentum showed negative, 60min on rb/ag is structurally mean-reverting.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from typing import Any  # noqa: E402

import pandas as pd  # noqa: E402

from research.wfa_ag_batch import AG_BT_KWARGS, AG_CONTRACTS  # noqa: E402
from research.wfa_rb_batch import BT_KWARGS as RB_BT_KWARGS  # noqa: E402
from research.wfa_rb_batch import CONTRACTS as RB_CONTRACTS  # noqa: E402
from research.wfa_rb_batch import run_batch, summarize  # noqa: E402

PARAM_GRID: dict[str, list[Any]] = {
    "boll_window": [20, 30, 40],
    "boll_dev": [2.0, 2.5, 3.0],
}


def main() -> int:
    from strategies.boll_reversal_strategy import BollReversalStrategy

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 80}\n# BollReversal on RB (60min)\n{'#' * 80}")
    rb = run_batch(
        strategy_class=BollReversalStrategy,
        param_grid=PARAM_GRID,
        fixed_params={"fixed_size": 1},
        label="BollRev/RB",
        contracts=RB_CONTRACTS,
        train_days=120,
        test_days=60,
        step_days=60,
        bt_kwargs=RB_BT_KWARGS,
        interval="1h",
        min_trades=10,
    )

    print(f"\n{'#' * 80}\n# BollReversal on AG (60min)\n{'#' * 80}")
    ag = run_batch(
        strategy_class=BollReversalStrategy,
        param_grid=PARAM_GRID,
        fixed_params={"fixed_size": 1},
        label="BollRev/AG",
        contracts=AG_CONTRACTS,
        train_days=80,
        test_days=30,
        step_days=30,
        bt_kwargs=AG_BT_KWARGS,
        interval="1h",
        min_trades=10,
    )

    if rb.empty and ag.empty:
        print("Both BollReversal batches failed.")
        return 1

    combined = pd.concat([df for df in [rb, ag] if not df.empty], ignore_index=True)
    pd.set_option("display.width", 230)
    pd.set_option("display.max_columns", 30)

    print(f"\n{'=' * 110}\nALL FOLDS\n{'=' * 110}")
    print(combined.to_string(index=False))

    print(f"\n{'=' * 110}\nBOLLREVERSAL SUMMARY (60min)\n{'=' * 110}")
    summaries = []
    if not rb.empty:
        summaries.append(summarize(rb, "BollRev/RB"))
    if not ag.empty:
        summaries.append(summarize(ag, "BollRev/AG"))
    summary_df = pd.DataFrame(summaries).set_index("strategy")
    print(summary_df.T.round(3).to_string())

    print(
        f"\n{'=' * 110}\nFULL CROSS-STRATEGY COMPARISON (60min, IS-OOS corr is the key column)\n{'=' * 110}"
    )
    benchmarks = [
        ("DoubleMa/RB", 0.058, -0.418, 50),
        ("Donchian/RB", -1.064, -0.791, 25),
        ("DoubleMa/AG", -1.427, -0.006, 25),
        ("Donchian/AG", -0.147, +0.516, 50),
    ]
    print(f"  {'config':16s} {'OOS Sharpe mean':>18s} {'IS-OOS corr':>14s} {'OOS positive %':>16s}")
    print("  " + "-" * 70)
    if not rb.empty:
        oos = rb["oos_sharpe"].dropna()
        corr = rb[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]
        print(
            f"  {'BollRev/RB':16s} {oos.mean():>+18.3f} {corr:>+14.3f} {(oos > 0).mean()*100:>15.0f}%"
        )
    if not ag.empty:
        oos = ag["oos_sharpe"].dropna()
        corr = ag[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]
        print(
            f"  {'BollRev/AG':16s} {oos.mean():>+18.3f} {corr:>+14.3f} {(oos > 0).mean()*100:>15.0f}%"
        )
    print("  " + "-" * 70)
    for label, oos, corr, pos in benchmarks:
        print(f"  {label:16s} {oos:>+18.3f} {corr:>+14.3f} {pos:>15.0f}%")

    print(f"\n{'=' * 110}\nPER-CONTRACT × STRATEGY (OOS Sharpe)\n{'=' * 110}")
    pivot = combined.pivot_table(
        index="contract", columns="strategy", values="oos_sharpe", aggfunc="mean"
    ).round(3)
    print(pivot.to_string())

    print(f"\n{'=' * 110}\nIS-PICKED PARAMS\n{'=' * 110}")
    for label, df in [("BollRev/RB", rb), ("BollRev/AG", ag)]:
        if df.empty:
            continue
        print(f"\n  {label}:")
        for p, n in df["best_params"].astype(str).value_counts().items():
            print(f"    {p}: {n} fold(s)")

    out_path = REPO_ROOT / "research" / "wfa_results_boll.csv"
    combined.to_csv(out_path, index=False)
    print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
