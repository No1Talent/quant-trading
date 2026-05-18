"""Run WFA on multiple delisted rb contracts independently; aggregate cross-contract.

Each contract is treated as a fully independent sample — no rollover stitching,
no continuous-contract back-adjustment.

Compares DoubleMa vs Donchian on the same 4 contracts to answer: is rb 60min
momentum-untradable in general, or is it just DoubleMa that fails here?
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

import pandas as pd  # noqa: E402

from research.wfa import run_wfa  # noqa: E402

CONTRACTS: list[tuple[str, datetime, datetime]] = [
    ("rb2310.SHFE", datetime(2023, 1, 31), datetime(2023, 10, 16)),
    ("rb2401.SHFE", datetime(2023, 5, 5), datetime(2024, 1, 15)),
    ("rb2405.SHFE", datetime(2023, 8, 23), datetime(2024, 5, 15)),
    ("rb2410.SHFE", datetime(2024, 1, 22), datetime(2024, 10, 15)),
]

BT_KWARGS = dict(
    capital=1_000_000,
    rate=1e-4,
    slippage=1,
    size=10,
    pricetick=1,
)


def run_batch(
    strategy_class: type,
    param_grid: dict[str, list[Any]],
    fixed_params: dict[str, Any],
    label: str,
    contracts: list[tuple[str, datetime, datetime]] | None = None,
    train_days: int = 120,
    test_days: int = 60,
    step_days: int = 60,
    bt_kwargs: dict[str, Any] | None = None,
    interval: str = "1h",
    min_trades: int = 10,
) -> pd.DataFrame:
    """Run WFA across multiple contracts for one strategy. Returns combined DataFrame."""
    if contracts is None:
        contracts = CONTRACTS
    if bt_kwargs is None:
        bt_kwargs = BT_KWARGS
    all_dfs = []
    for vt_symbol, start, end in contracts:
        print(f"  [{label}] {vt_symbol}  {start.date()} → {end.date()}")
        try:
            df = run_wfa(
                strategy_class=strategy_class,
                param_grid=param_grid,
                fixed_params=fixed_params,
                vt_symbol=vt_symbol,
                interval=interval,
                start=start,
                end=end,
                train_days=train_days,
                test_days=test_days,
                step_days=step_days,
                metric="sharpe_ratio",
                min_trades=min_trades,
                **bt_kwargs,
            )
            df.insert(0, "contract", vt_symbol)
            df.insert(0, "strategy", label)
            all_dfs.append(df)
        except Exception as e:
            print(f"    SKIPPED: {type(e).__name__}: {e}")

    if not all_dfs:
        return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True)


# Back-compat alias
run_rb_batch = run_batch


def summarize(df: pd.DataFrame, label: str) -> dict[str, Any]:
    oos = df["oos_sharpe"].dropna()
    is_ = df["is_sharpe"].dropna()
    corr = df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]
    return {
        "strategy": label,
        "folds": len(df),
        "oos_sharpe_mean": oos.mean(),
        "oos_sharpe_median": oos.median(),
        "oos_sharpe_std": oos.std(),
        "oos_sharpe_min": oos.min(),
        "oos_sharpe_max": oos.max(),
        "oos_positive_pct": (oos > 0).mean() * 100,
        "is_sharpe_mean": is_.mean(),
        "is_oos_decay": oos.mean() - is_.mean(),
        "is_oos_corr": corr,
        "total_oos_return_pct": df["oos_return_pct"].sum(),
    }


def main() -> int:
    from strategies.donchian_strategy import DonchianStrategy
    from strategies.double_ma_strategy import DoubleMaStrategy

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 80}\n# DoubleMa batch\n{'#' * 80}")
    dm = run_batch(
        strategy_class=DoubleMaStrategy,
        param_grid={"fast_window": [5, 10, 15], "slow_window": [20, 30, 40]},
        fixed_params={"fixed_size": 1},
        label="DoubleMa",
    )

    print(f"\n{'#' * 80}\n# Donchian batch\n{'#' * 80}")
    dn = run_batch(
        strategy_class=DonchianStrategy,
        param_grid={"entry_window": [20, 30, 40], "exit_window": [5, 10, 15]},
        fixed_params={"fixed_size": 1},
        label="Donchian",
    )

    if dm.empty and dn.empty:
        print("Both strategies failed to produce results.")
        return 1

    combined = pd.concat([dm, dn], ignore_index=True)

    pd.set_option("display.width", 230)
    pd.set_option("display.max_columns", 30)

    print(f"\n{'=' * 110}\nALL FOLDS\n{'=' * 110}")
    print(combined.to_string(index=False))

    print(f"\n{'=' * 110}\nSTRATEGY COMPARISON\n{'=' * 110}")
    summaries = []
    if not dm.empty:
        summaries.append(summarize(dm, "DoubleMa"))
    if not dn.empty:
        summaries.append(summarize(dn, "Donchian"))
    summary_df = pd.DataFrame(summaries).set_index("strategy")
    print(summary_df.T.round(3).to_string())

    # Per-contract per-strategy
    print(f"\n{'=' * 110}\nPER-CONTRACT × STRATEGY (OOS Sharpe mean)\n{'=' * 110}")
    pivot = combined.pivot_table(
        index="contract", columns="strategy", values="oos_sharpe", aggfunc="mean"
    ).round(3)
    print(pivot.to_string())

    # Param distribution comparison
    print(f"\n{'=' * 110}\nIS-PICKED PARAMS (top per strategy)\n{'=' * 110}")
    for label, df in [("DoubleMa", dm), ("Donchian", dn)]:
        if df.empty:
            continue
        print(f"\n  {label}:")
        for p, n in df["best_params"].astype(str).value_counts().items():
            print(f"    {p}: {n} fold(s)")

    out_path = REPO_ROOT / "research" / "wfa_results_rb_compare.csv"
    combined.to_csv(out_path, index=False)
    print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
