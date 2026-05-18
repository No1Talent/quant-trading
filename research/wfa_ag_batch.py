"""Same DoubleMa+Donchian WFA, but on AG (silver) instead of RB (rebar).

Question: is DoubleMa untradable specifically on rb 60min, or on all commodity
60min? AG is structurally maximally different from rb — precious metal, macro-
driven, different liquidity profile, longer night session.

If AG shows similar pattern (OOS Sharpe ~0, IS-OOS corr ≈ 0 or negative),
the strategy class is the issue, not the instrument. If AG shows clear positive
edge, rb 60min specifically lacks tradeable trends.
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

AG_CONTRACTS: list[tuple[str, datetime, datetime]] = [
    ("ag2206.SHFE", datetime(2022, 1, 6), datetime(2022, 6, 15)),
    ("ag2212.SHFE", datetime(2022, 7, 18), datetime(2022, 12, 15)),
    ("ag2306.SHFE", datetime(2023, 1, 11), datetime(2023, 6, 15)),
    ("ag2312.SHFE", datetime(2023, 7, 17), datetime(2023, 12, 15)),
    ("ag2406.SHFE", datetime(2024, 1, 4), datetime(2024, 6, 17)),
    ("ag2410.SHFE", datetime(2024, 5, 11), datetime(2024, 10, 15)),
    ("ag2502.SHFE", datetime(2024, 9, 4), datetime(2025, 2, 17)),
    ("ag2506.SHFE", datetime(2025, 1, 7), datetime(2025, 6, 16)),
]

# AG (silver) contract: 15 kg/lot, pricetick 1 RMB, commission ~5e-5
AG_BT_KWARGS = dict(
    capital=1_000_000,
    rate=5e-5,
    slippage=1,
    size=15,
    pricetick=1,
)


def main() -> int:
    from strategies.donchian_strategy import DonchianStrategy
    from strategies.double_ma_strategy import DoubleMaStrategy

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    # AG covers ~5 months/contract → tighter windows
    print(f"\n{'#' * 80}\n# DoubleMa on AG\n{'#' * 80}")
    dm = run_batch(
        strategy_class=DoubleMaStrategy,
        param_grid={"fast_window": [5, 10, 15], "slow_window": [20, 30, 40]},
        fixed_params={"fixed_size": 1},
        label="DoubleMa",
        contracts=AG_CONTRACTS,
        train_days=80,
        test_days=30,
        step_days=30,
        bt_kwargs=AG_BT_KWARGS,
    )

    print(f"\n{'#' * 80}\n# Donchian on AG\n{'#' * 80}")
    dn = run_batch(
        strategy_class=DonchianStrategy,
        param_grid={"entry_window": [20, 30, 40], "exit_window": [5, 10, 15]},
        fixed_params={"fixed_size": 1},
        label="Donchian",
        contracts=AG_CONTRACTS,
        train_days=80,
        test_days=30,
        step_days=30,
        bt_kwargs=AG_BT_KWARGS,
    )

    if dm.empty and dn.empty:
        print("Both AG batches failed.")
        return 1

    combined = pd.concat([dm, dn], ignore_index=True)
    pd.set_option("display.width", 230)
    pd.set_option("display.max_columns", 30)

    print(f"\n{'=' * 110}\nAG FOLDS\n{'=' * 110}")
    print(combined.to_string(index=False))

    print(f"\n{'=' * 110}\nAG vs RB COMPARISON\n{'=' * 110}")
    summaries = []
    if not dm.empty:
        summaries.append(summarize(dm, "DoubleMa (AG)"))
    if not dn.empty:
        summaries.append(summarize(dn, "Donchian (AG)"))
    # Add RB benchmark from prior run for direct comparison
    summaries.append(
        {
            "strategy": "DoubleMa (RB ref)",
            "folds": 8,
            "oos_sharpe_mean": 0.058,
            "oos_sharpe_median": 0.026,
            "oos_sharpe_std": 2.545,
            "oos_sharpe_min": -2.805,
            "oos_sharpe_max": 3.404,
            "oos_positive_pct": 50.0,
            "is_sharpe_mean": 0.848,
            "is_oos_decay": -0.790,
            "is_oos_corr": -0.418,
            "total_oos_return_pct": 0.035,
        }
    )
    summaries.append(
        {
            "strategy": "Donchian (RB ref)",
            "folds": 8,
            "oos_sharpe_mean": -1.064,
            "oos_sharpe_median": -1.319,
            "oos_sharpe_std": 1.453,
            "oos_sharpe_min": -3.005,
            "oos_sharpe_max": 0.938,
            "oos_positive_pct": 25.0,
            "is_sharpe_mean": -0.109,
            "is_oos_decay": -0.955,
            "is_oos_corr": -0.791,
            "total_oos_return_pct": -0.247,
        }
    )
    summary_df = pd.DataFrame(summaries).set_index("strategy")
    print(summary_df.T.round(3).to_string())

    print(f"\n{'=' * 110}\nPER-CONTRACT × STRATEGY (AG OOS Sharpe mean)\n{'=' * 110}")
    pivot = combined.pivot_table(
        index="contract", columns="strategy", values="oos_sharpe", aggfunc="mean"
    ).round(3)
    print(pivot.to_string())

    print(f"\n{'=' * 110}\nIS-PICKED PARAMS\n{'=' * 110}")
    for label, df in [("DoubleMa", dm), ("Donchian", dn)]:
        if df.empty:
            continue
        print(f"\n  {label}:")
        for p, n in df["best_params"].astype(str).value_counts().items():
            print(f"    {p}: {n} fold(s)")

    out_path = REPO_ROOT / "research" / "wfa_results_ag.csv"
    combined.to_csv(out_path, index=False)
    print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
