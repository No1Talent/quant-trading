"""Donchian on AG 60min — 8-contract pressure test of the +0.52 IS-OOS correlation.

Original Donchian/AG (4 contracts, 8 folds) showed:
  OOS Sharpe mean -0.15, OOS positive 50%, IS-OOS corr +0.52
  But ag2406 alone produced +1.76 OOS Sharpe, carrying most of the positive signal.

Question: does corr +0.52 survive when we double the sample to 8 contracts
covering 2022-01 to 2025-06 (including 2022 silver spike, 2023 Fed tightening,
2024-2025 silver bull)? Or does it collapse toward zero/negative, revealing
ag2406 as a single-regime lucky sample?

Control variables (identical to the original 4-contract run):
  param grid: entry [20,30,40] x exit [5,10,15]
  windows: train=80, test=30, step=30 calendar days
  min_trades=10, bt_kwargs matches AG (size=15, rate=5e-5)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from research.wfa_ag_batch import AG_BT_KWARGS, AG_CONTRACTS  # noqa: E402
from research.wfa_rb_batch import run_batch  # noqa: E402

# Baseline from prior 4-contract run for direct comparison
BASELINE_4_CONTRACT = {
    "folds": 8,
    "oos_sharpe_mean": -0.147,
    "oos_sharpe_median": -0.714,
    "oos_positive_pct": 50.0,
    "is_sharpe_mean": 1.611,
    "is_oos_corr": 0.516,
    "total_oos_return_pct": 1.184,
}


def main() -> int:
    from strategies.donchian_strategy import DonchianStrategy

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 80}\n# Donchian on AG (8 contracts) — deep dive\n{'#' * 80}")
    print(f"  Contracts: {[c[0] for c in AG_CONTRACTS]}")

    df = run_batch(
        strategy_class=DonchianStrategy,
        param_grid={"entry_window": [20, 30, 40], "exit_window": [5, 10, 15]},
        fixed_params={"fixed_size": 1},
        label="Donchian",
        contracts=AG_CONTRACTS,
        train_days=80,
        test_days=30,
        step_days=30,
        bt_kwargs=AG_BT_KWARGS,
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
    if corr > 0.4 and pos_pct >= 50:
        print("  [STRONG] IS-OOS corr held; the +0.52 was not a 4-contract artifact.")
        print("           Donchian/AG 60min likely has a real (if modest) systematic edge.")
        print("           Next: parameter robustness / regime breakdown / cost sensitivity.")
    elif corr > 0.2 and pos_pct >= 50:
        print("  [MODERATE] Corr softened but stayed positive. Edge plausible but smaller")
        print("             than 4-contract suggested. Worth deeper analysis but not")
        print("             ready for capital.")
    elif corr > 0:
        print("  [WEAK] Corr dropped sharply when sample doubled. Original +0.52 was")
        print("         heavily contributed by ag2406's single-regime luck. Edge is")
        print("         marginal at best.")
    else:
        print("  [GONE] Corr collapsed to <= 0. The +0.52 was a 4-contract artifact;")
        print("         ag2406 carried the entire signal. Donchian/AG 60min joins the")
        print("         'no consistent alpha' bucket. Move on.")

    out_path = REPO_ROOT / "research" / "wfa_results_ag_donchian_deepdive.csv"
    df.to_csv(out_path, index=False)
    print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
