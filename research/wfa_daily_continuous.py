"""G2: Daily WFA on rb/ag main-continuous contracts.

First test in the daily timeframe — finally enabled by AkShare's main-continuous
data (RB0: 4158 daily bars from 2009; AG0: 3408 from 2012). The single-contract
daily data was too short (~240 bars/contract) for any standard WFA window;
continuous data has 14-17 years of history.

Caveat: main-continuous data has roll-over gaps every ~2-4 months (when the
active month switches). For DAILY momentum/breakout strategies these typically
get absorbed into bar #N's price action and don't reliably mimic same-contract
flow. Treating this run as a rough first pass; rollover-adjusted continuous
would be a follow-up if results justify.

WFA windows: train=700 / test=250 / step=250 calendar days
- RB: ~23 folds, AG: ~17 folds → ~80 fold-evals across 2 strategies
- Non-overlapping test windows (step == test)

Grids capped at slow=100 to leave trading time after ArrayManager warmup in
each fresh OOS backtest (250 calendar days = ~170 trading days; with slow=100
warmup, ~70 days to trade — enough for 5-15 momentum trades).
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

RB_CONTINUOUS: list[tuple[str, datetime, datetime]] = [
    ("rb_continuous.SHFE", datetime(2009, 3, 27), datetime(2026, 5, 15)),
]
AG_CONTINUOUS: list[tuple[str, datetime, datetime]] = [
    ("ag_continuous.SHFE", datetime(2012, 5, 10), datetime(2026, 5, 15)),
]

# Contract specs (same as 60min runs for direct comparison)
RB_BT = dict(capital=1_000_000, rate=1e-4, slippage=1, size=10, pricetick=1)
AG_BT = dict(capital=1_000_000, rate=5e-5, slippage=1, size=15, pricetick=1)

TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250

# Daily-appropriate grids. Capped at slow=100 due to load_bar warmup constraint
# in test windows (~170 trading days available after warmup with slow=100).
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
DN_GRID = {"entry_window": [20, 40, 80], "exit_window": [10, 20, 40]}

# Daily strategies fire fewer signals than 60min — relax min_trades
MIN_TRADES = 5


def main() -> int:
    from strategies.donchian_strategy import DonchianStrategy
    from strategies.double_ma_strategy import DoubleMaStrategy

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    runs = [
        ("DoubleMa/RB", DoubleMaStrategy, DM_GRID, RB_CONTINUOUS, RB_BT),
        ("Donchian/RB", DonchianStrategy, DN_GRID, RB_CONTINUOUS, RB_BT),
        ("DoubleMa/AG", DoubleMaStrategy, DM_GRID, AG_CONTINUOUS, AG_BT),
        ("Donchian/AG", DonchianStrategy, DN_GRID, AG_CONTINUOUS, AG_BT),
    ]

    all_dfs = []
    for label, cls, grid, contracts, bt in runs:
        print(f"\n{'#' * 80}\n# {label} (daily continuous)\n{'#' * 80}")
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
            min_trades=MIN_TRADES,
        )
        if not df.empty:
            all_dfs.append(df)

    if not all_dfs:
        print("All daily-continuous batches failed.")
        return 1

    combined = pd.concat(all_dfs, ignore_index=True)
    pd.set_option("display.width", 230)
    pd.set_option("display.max_columns", 30)

    print(f"\n{'=' * 110}\nALL DAILY-CONTINUOUS FOLDS\n{'=' * 110}")
    print(combined.to_string(index=False))

    print(f"\n{'=' * 110}\nDAILY-CONTINUOUS SUMMARY\n{'=' * 110}")
    summaries = []
    for label, _, _, _, _ in runs:
        sub = combined[combined["strategy"] == label]
        if not sub.empty:
            summaries.append(summarize(sub, label))
    summary_df = pd.DataFrame(summaries).set_index("strategy")
    print(summary_df.T.round(3).to_string())

    print(f"\n{'=' * 110}\nDAILY vs 60MIN HEADLINE (OOS Sharpe mean, IS-OOS corr)\n{'=' * 110}")
    benchmarks_60min = {
        "DoubleMa/RB 60min (8 contracts)": (0.058, -0.418),
        "Donchian/RB 60min (8 contracts)": (-1.064, -0.791),
        "DoubleMa/AG 60min (8 contracts)": (-1.427, -0.006),
        "Donchian/AG 60min (8 contracts, full)": (0.180, 0.138),
    }
    print(f"  {'config':40s} {'OOS Sharpe mean':>18s} {'IS-OOS corr':>14s}")
    print("  " + "-" * 74)
    for label, _, _, _, _ in runs:
        sub = combined[combined["strategy"] == label]
        if sub.empty:
            continue
        oos = sub["oos_sharpe"].dropna().mean()
        corr = sub[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]
        n = len(sub)
        print(f"  {label + f' daily ({n} folds)':40s} {oos:>+18.3f} {corr:>+14.3f}")
    print("  " + "-" * 74)
    for label, (oos, corr) in benchmarks_60min.items():
        print(f"  {label:40s} {oos:>+18.3f} {corr:>+14.3f}")

    print(f"\n{'=' * 110}\nPER-FOLD OOS BREAKDOWN (look for regime patterns)\n{'=' * 110}")
    sample_cols = [
        "strategy",
        "fold",
        "train_end",
        "test_end",
        "best_params",
        "is_sharpe",
        "is_trades",
        "oos_sharpe",
        "oos_return_pct",
        "oos_trades",
    ]
    print(combined[sample_cols].to_string(index=False))

    print(f"\n{'=' * 110}\nVERDICT\n{'=' * 110}")
    best_oos = 0
    best_label = "(none)"
    for label, _, _, _, _ in runs:
        sub = combined[combined["strategy"] == label]
        if sub.empty:
            continue
        oos = sub["oos_sharpe"].dropna()
        corr = sub[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]
        if oos.mean() > best_oos:
            best_oos = oos.mean()
            best_label = label
        if oos.mean() > 0.3 and corr > 0.2:
            print(f"  [WIN] {label}: OOS mean +{oos.mean():.3f}, corr +{corr:.3f}.")
            print("        Daily timeframe shows tradeable structure where 60min did not.")
    if best_oos < 0.3:
        print(f"  [NO_WIN] Best was {best_label} with OOS Sharpe +{best_oos:.3f}.")
        print(
            "           Daily continuous on RB/AG also fails to show robust momentum/breakout alpha."
        )
        print(
            "           Consider: rollover-adjusted data, weekly timeframe, or cross-sectional approach."
        )

    out_path = REPO_ROOT / "research" / "wfa_results_daily_continuous.csv"
    combined.to_csv(out_path, index=False)
    print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
