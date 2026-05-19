"""Walk-Forward Analysis wrapper around backtest_runner.

For each (train, test) window:
  1. grid-search the param space on the train window → pick best by `metric`
  2. lock those params and run on the test window → record OOS stats

Output: a DataFrame with one row per fold (train range, test range, best params,
IS stats, OOS stats). The whole point is to see whether the IS-optimal params
hold up OOS, or whether the strategy is overfit.

Grid is exhaustive — fine for 2-3 parameters with 4-6 values each. For bigger
spaces, swap in random search or hand off to vn.py's GeneticOptimization.
"""

from __future__ import annotations

import itertools
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from research.backtest_runner import run_backtest  # noqa: E402

logger = logging.getLogger("wfa")


@dataclass
class Window:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


def make_windows(
    start: datetime,
    end: datetime,
    train_days: int,
    test_days: int,
    step_days: int,
) -> list[Window]:
    """Rolling windows. train_end == test_start (no gap)."""
    windows = []
    cursor = start
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        windows.append(Window(train_start, train_end, test_start, test_end))
        cursor = cursor + timedelta(days=step_days)
    return windows


def grid_search(
    strategy_class: type,
    param_grid: dict[str, list[Any]],
    fixed_params: dict[str, Any],
    vt_symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    metric: str = "sharpe_ratio",
    min_trades: int = 10,
    **bt_kwargs: Any,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Exhaustive grid search. Returns (best_params, best_stats, all_results).

    `min_trades` filters out param combos with too few IS trades — a Sharpe
    computed on 2 lucky trades is noise, not signal.
    """
    keys = list(param_grid.keys())
    combos = list(itertools.product(*[param_grid[k] for k in keys]))

    all_results = []
    for combo in combos:
        params = dict(fixed_params)
        params.update(dict(zip(keys, combo)))
        try:
            stats = run_backtest(
                strategy_class=strategy_class,
                params=params,
                vt_symbol=vt_symbol,
                interval=interval,
                start=start,
                end=end,
                **bt_kwargs,
            )
        except Exception as e:
            logger.warning("Backtest failed for %s: %s", params, e)
            continue

        all_results.append({"params": params, "stats": stats})

    scored = [
        r
        for r in all_results
        if r["stats"].get(metric) is not None
        and r["stats"].get("total_trade_count", 0) >= min_trades
    ]
    if not scored:
        n_with_metric = sum(1 for r in all_results if r["stats"].get(metric) is not None)
        raise RuntimeError(
            f"No param combo met min_trades={min_trades} (metric={metric}). "
            f"Tried {len(combos)} combos, {n_with_metric} returned a metric value."
        )

    best = max(scored, key=lambda r: r["stats"][metric])
    return best["params"], best["stats"], all_results


def run_wfa(
    strategy_class: type,
    param_grid: dict[str, list[Any]],
    fixed_params: dict[str, Any],
    vt_symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    train_days: int,
    test_days: int,
    step_days: int,
    metric: str = "sharpe_ratio",
    min_trades: int = 10,
    return_curves: bool = False,
    skip_empty_folds: bool = False,
    **bt_kwargs: Any,
):
    """Roll forward: grid-search IS, lock best params, run OOS. One row per fold.

    If `return_curves=True`, returns (df, curves) where `curves` is a list of
    dicts (one per fold) with keys `fold`, `is_daily_df`, `oos_daily_df`. The
    IS curve is obtained by re-running the winning param set with
    return_daily_df=True (one extra train backtest per fold). Used by ensemble
    research to compute per-fold inverse-vol weights and daily PnL combines.

    If `skip_empty_folds=True`, folds where no param combo meets `min_trades`
    are logged and skipped rather than raising. Use for sparse strategies
    (e.g. event-gated) where early folds may have insufficient activity.
    """
    windows = make_windows(start, end, train_days, test_days, step_days)
    if not windows:
        raise ValueError(
            f"No windows produced. data range={end - start}, "
            f"required={train_days + test_days} days"
        )

    logger.info(
        "WFA: %d folds (train=%dd / test=%dd / step=%dd, min_trades=%d)",
        len(windows),
        train_days,
        test_days,
        step_days,
        min_trades,
    )

    rows = []
    curves: list[dict[str, Any]] = []
    for i, w in enumerate(windows, 1):
        logger.info(
            "Fold %d/%d: train %s→%s | test %s→%s",
            i,
            len(windows),
            w.train_start.date(),
            w.train_end.date(),
            w.test_start.date(),
            w.test_end.date(),
        )

        try:
            best_params, is_stats, _ = grid_search(
                strategy_class=strategy_class,
                param_grid=param_grid,
                fixed_params=fixed_params,
                vt_symbol=vt_symbol,
                interval=interval,
                start=w.train_start,
                end=w.train_end,
                metric=metric,
                min_trades=min_trades,
                **bt_kwargs,
            )
        except RuntimeError as e:
            if skip_empty_folds:
                logger.warning(
                    "Fold %d skipped (no IS combo met min_trades=%d): %s", i, min_trades, e
                )
                continue
            raise

        if return_curves:
            oos_stats, oos_daily = run_backtest(
                strategy_class=strategy_class,
                params=best_params,
                vt_symbol=vt_symbol,
                interval=interval,
                start=w.test_start,
                end=w.test_end,
                return_daily_df=True,
                **bt_kwargs,
            )
            _, is_daily = run_backtest(
                strategy_class=strategy_class,
                params=best_params,
                vt_symbol=vt_symbol,
                interval=interval,
                start=w.train_start,
                end=w.train_end,
                return_daily_df=True,
                **bt_kwargs,
            )
            curves.append({"fold": i, "is_daily_df": is_daily, "oos_daily_df": oos_daily})
        else:
            oos_stats = run_backtest(
                strategy_class=strategy_class,
                params=best_params,
                vt_symbol=vt_symbol,
                interval=interval,
                start=w.test_start,
                end=w.test_end,
                **bt_kwargs,
            )

        rows.append(
            {
                "fold": i,
                "train_start": w.train_start.date(),
                "train_end": w.train_end.date(),
                "test_start": w.test_start.date(),
                "test_end": w.test_end.date(),
                "best_params": {k: best_params[k] for k in param_grid},
                "is_sharpe": is_stats.get("sharpe_ratio"),
                "is_return_pct": is_stats.get("total_return"),
                "is_trades": is_stats.get("total_trade_count"),
                "oos_sharpe": oos_stats.get("sharpe_ratio"),
                "oos_return_pct": oos_stats.get("total_return"),
                "oos_max_dd_pct": oos_stats.get("max_ddpercent"),
                "oos_trades": oos_stats.get("total_trade_count"),
            }
        )

    df = pd.DataFrame(rows)
    if return_curves:
        return df, curves
    return df


def main() -> int:
    """WFA on DoubleMa over rb2410 60min."""
    from strategies.double_ma_strategy import DoubleMaStrategy

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # Suppress vn.py engine's tqdm-style noisy output
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.WARNING)

    df = run_wfa(
        strategy_class=DoubleMaStrategy,
        param_grid={
            "fast_window": [5, 10, 15],
            "slow_window": [20, 30, 40],
        },
        fixed_params={"fixed_size": 1},
        vt_symbol="rb2410.SHFE",
        interval="1h",
        start=datetime(2024, 1, 22),
        end=datetime(2024, 10, 15),
        train_days=120,
        test_days=60,
        step_days=60,
        metric="sharpe_ratio",
        min_trades=10,
        capital=1_000_000,
        rate=1e-4,
        slippage=1,
        size=10,
        pricetick=1,
    )

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print("\n" + "=" * 100)
    print("WFA RESULTS: DoubleMa on rb2410.SHFE 1h")
    print("=" * 100)
    print(df.to_string(index=False))

    # Aggregate
    print("\n" + "=" * 100)
    print("OOS SUMMARY")
    print("=" * 100)
    oos_sharpes = df["oos_sharpe"].dropna()
    print(f"  folds:                  {len(df)}")
    print(f"  OOS Sharpe mean:        {oos_sharpes.mean():.3f}")
    print(f"  OOS Sharpe median:      {oos_sharpes.median():.3f}")
    print(f"  OOS Sharpe min/max:     {oos_sharpes.min():.3f} / {oos_sharpes.max():.3f}")
    print(f"  OOS positive folds:     {(oos_sharpes > 0).sum()} / {len(oos_sharpes)}")
    print(
        f"  IS→OOS Sharpe decay:    "
        f"{df['is_sharpe'].mean():.3f} → {oos_sharpes.mean():.3f} "
        f"({(oos_sharpes.mean() - df['is_sharpe'].mean()):.3f})"
    )

    out_path = "research/wfa_results_doublema_rb2410.csv"
    df.to_csv(out_path, index=False)
    print(f"\nResults saved → {out_path}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
