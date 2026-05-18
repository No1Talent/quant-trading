"""Daily WFA on rb + ag — test if timeframe (60min) was the issue.

Same DoubleMa + Donchian strategies, same 4 rb + 4 ag delisted contracts,
but on DAILY bars instead of 60min. Academic literature finds time-series
momentum works on daily/weekly bars but not intraday — this test directly
confronts that for our universe.

If daily shows IS-OOS Sharpe correlation > 0.3 and OOS positive >60%, the
prior 60min "no alpha" finding is recharacterized as a timeframe selection
problem, not a strategy problem. If daily looks just as bad, time-series
momentum on these instruments is broken regardless of timeframe.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from research.wfa_rb_batch import run_batch, summarize  # noqa: E402

# Daily contracts span ~12 months each (vs ~5-9mo for 60min)
RB_DAILY: list[tuple[str, datetime, datetime]] = [
    ("rb2310.SHFE", datetime(2022, 10, 18), datetime(2023, 10, 16)),
    ("rb2401.SHFE", datetime(2023, 1, 17), datetime(2024, 1, 15)),
    ("rb2405.SHFE", datetime(2023, 5, 16), datetime(2024, 5, 15)),
    ("rb2410.SHFE", datetime(2023, 10, 17), datetime(2024, 10, 15)),
]

AG_DAILY: list[tuple[str, datetime, datetime]] = [
    ("ag2306.SHFE", datetime(2022, 6, 16), datetime(2023, 6, 15)),
    ("ag2312.SHFE", datetime(2022, 12, 16), datetime(2023, 12, 15)),
    ("ag2406.SHFE", datetime(2023, 6, 16), datetime(2024, 6, 17)),
    ("ag2410.SHFE", datetime(2023, 10, 17), datetime(2024, 10, 15)),
]

# Daily windows: ~240 trading days / contract. train=180/test=60/step=60 → 1-2 folds
# Use train=150 for more folds (3-4 per contract)
TRAIN_DAYS = 150
TEST_DAYS = 60
STEP_DAYS = 60

# Daily grids: shorter windows since 1 bar = 1 trading day
# Need slow_window << test_days (60 cal days ≈ 42 trading days). Cap at 30
DM_GRID_DAILY = {"fast_window": [3, 5, 10], "slow_window": [10, 20, 30]}
DN_GRID_DAILY = {"entry_window": [10, 20, 30], "exit_window": [3, 5, 10]}

RB_BT = dict(capital=1_000_000, rate=1e-4, slippage=1, size=10, pricetick=1)
AG_BT = dict(capital=1_000_000, rate=5e-5, slippage=1, size=15, pricetick=1)


def main() -> int:
    from strategies.donchian_strategy import DonchianStrategy
    from strategies.double_ma_strategy import DoubleMaStrategy

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    runs = [
        ("DoubleMa/RB", DoubleMaStrategy, DM_GRID_DAILY, RB_DAILY, RB_BT),
        ("Donchian/RB", DonchianStrategy, DN_GRID_DAILY, RB_DAILY, RB_BT),
        ("DoubleMa/AG", DoubleMaStrategy, DM_GRID_DAILY, AG_DAILY, AG_BT),
        ("Donchian/AG", DonchianStrategy, DN_GRID_DAILY, AG_DAILY, AG_BT),
    ]

    all_dfs = []
    for label, cls, grid, contracts, bt in runs:
        print(f"\n{'#' * 80}\n# {label} (daily)\n{'#' * 80}")
        df = run_batch(
            strategy_class=cls,
            param_grid=grid,
            fixed_params={"fixed_size": 1},
            label=label,
            contracts=contracts,
            train_days=TRAIN_DAYS,
            test_days=TEST_DAYS,
            step_days=STEP_DAYS,
            bt_kwargs=bt,
            interval="1d",
            min_trades=5,  # daily signals are sparse — 5 ≈ 2 full position cycles
        )
        if not df.empty:
            all_dfs.append(df)

    if not all_dfs:
        print("All daily batches failed.")
        return 1

    combined = pd.concat(all_dfs, ignore_index=True)
    pd.set_option("display.width", 230)
    pd.set_option("display.max_columns", 30)

    print(f"\n{'=' * 110}\nALL DAILY FOLDS\n{'=' * 110}")
    print(combined.to_string(index=False))

    print(f"\n{'=' * 110}\nDAILY SUMMARY\n{'=' * 110}")
    summaries = []
    for label, _, _, _, _ in runs:
        sub = combined[combined["strategy"] == label]
        if not sub.empty:
            summaries.append(summarize(sub, label))
    summary_df = pd.DataFrame(summaries).set_index("strategy")
    print(summary_df.T.round(3).to_string())

    print(f"\n{'=' * 110}\nDAILY vs 60MIN HEADLINE (OOS Sharpe mean, IS-OOS corr)\n{'=' * 110}")
    benchmarks = {
        "DoubleMa/RB 60min": (0.058, -0.418),
        "Donchian/RB 60min": (-1.064, -0.791),
        "DoubleMa/AG 60min": (-1.427, -0.006),
        "Donchian/AG 60min": (-0.147, 0.516),
    }
    print(f"  {'config':22s} {'OOS Sharpe mean':>18s} {'IS-OOS corr':>14s}")
    print("  " + "-" * 56)
    for label, _, _, _, _ in runs:
        sub = combined[combined["strategy"] == label]
        if sub.empty:
            continue
        oos = sub["oos_sharpe"].dropna().mean()
        corr = sub[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]
        print(f"  {label + ' daily':22s} {oos:>+18.3f} {corr:>+14.3f}")
    print("  " + "-" * 56)
    for label, (oos, corr) in benchmarks.items():
        print(f"  {label:22s} {oos:>+18.3f} {corr:>+14.3f}")

    print(f"\n{'=' * 110}\nPER-CONTRACT × STRATEGY (Daily OOS Sharpe)\n{'=' * 110}")
    pivot = combined.pivot_table(
        index="contract", columns="strategy", values="oos_sharpe", aggfunc="mean"
    ).round(3)
    print(pivot.to_string())

    out_path = REPO_ROOT / "research" / "wfa_results_daily.csv"
    combined.to_csv(out_path, index=False)
    print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
