"""Combinatorial Purged Cross-Validation (CPCV) for trading WFA.

Adapts Marcos López de Prado's CPCV (Advances in Financial ML, ch. 7) to the
trading-WFA setting. Two important differences from textbook CPCV:

  1. Train-before-test ordering enforced. Trading P&L is path-dependent; we
     can't train on 2024 and test on 2018. Textbook CPCV freely combines folds;
     we restrict to splits where every train fold precedes the test fold.

  2. Purge & embargo defined in CALENDAR days, not "label horizon". For daily
     bars with rolling-window features (e.g. ma_20), the leak zone is approx
     `max(window)` days. Default purge_days=20 covers most reasonable rolling
     features. Embargo applies between non-adjacent splits when a fold could
     be re-used as train after being test elsewhere; for plain walk-forward
     (each test fold appears once), embargo is operationally a no-op but we
     keep the parameter for API parity with the full CPCV variant below.

Two functions:
  - `run_pwf`: Purged Walk-Forward. Each fold becomes test exactly once;
    train is all prior folds with purge gap. Returns N-K_train splits.
    This is the practical workhorse — drop-in replacement for `run_wfa`.

  - `run_cpcv_full`: All valid (train, test) combinations with k_test test
    folds. Yields O(N choose k_test) splits — denser OOS sampling. Use for
    PBO (Probability of Backtest Overfitting) estimates. Slower.

Reuses wfa.grid_search so the inner IS-grid-search behaviour is identical to
wfa.run_wfa — only the outer fold logic differs.
"""

from __future__ import annotations

import itertools
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from research.backtest_runner import run_backtest  # noqa: E402
from research.wfa import grid_search  # noqa: E402

logger = logging.getLogger("cpcv")


@dataclass
class Fold:
    fold_id: int
    start: datetime
    end: datetime


@dataclass
class Split:
    split_id: int
    train_intervals: list[tuple[datetime, datetime]]
    test_start: datetime
    test_end: datetime
    test_fold_ids: tuple[int, ...]
    train_fold_ids: tuple[int, ...] = field(default_factory=tuple)
    purge_dropped_days: int = 0
    embargo_dropped_days: int = 0


def partition_into_folds(start: datetime, end: datetime, n_folds: int) -> list[Fold]:
    """Equal-length contiguous folds over [start, end]. Last fold absorbs any
    rounding remainder so the partition is exhaustive."""
    total_days = (end - start).days
    fold_len = total_days // n_folds
    folds: list[Fold] = []
    for i in range(n_folds):
        f_start = start + timedelta(days=i * fold_len)
        f_end = end if i == n_folds - 1 else f_start + timedelta(days=fold_len)
        folds.append(Fold(fold_id=i, start=f_start, end=f_end))
    return folds


def build_pwf_splits(folds: list[Fold], purge_days: int, min_train_folds: int = 2) -> list[Split]:
    """Purged walk-forward: for each fold i ≥ min_train_folds, train = folds
    [0..i-1] (concatenated, with purge_days cut off the end), test = fold[i]."""
    splits: list[Split] = []
    for i in range(min_train_folds, len(folds)):
        train_start = folds[0].start
        train_end_raw = folds[i - 1].end
        train_end = train_end_raw - timedelta(days=purge_days)
        if train_end <= train_start:
            continue
        splits.append(
            Split(
                split_id=len(splits),
                train_intervals=[(train_start, train_end)],
                test_start=folds[i].start,
                test_end=folds[i].end,
                test_fold_ids=(i,),
                train_fold_ids=tuple(range(i)),
                purge_dropped_days=purge_days,
                embargo_dropped_days=0,
            )
        )
    return splits


def build_cpcv_splits(
    folds: list[Fold], k_test: int, purge_days: int, embargo_days: int
) -> list[Split]:
    """Full CPCV with K test folds chosen per split. Constraint: every train
    fold must precede every test fold (no future-leakage permutation).

    For N=10, k_test=2: walks through all (train_subset, test_subset) where
    max(train_ids) < min(test_ids). Train-end gets purged; train-start gets
    embargoed only if it overlaps a previous test (rare under ordering).

    Yields fewer splits than textbook CPCV (which allows non-ordered subsets),
    but enough density for variance estimation.
    """
    n = len(folds)
    splits: list[Split] = []
    test_combos = list(itertools.combinations(range(n), k_test))

    for test_ids in test_combos:
        # Ordering: every train fold must come BEFORE min(test_ids).
        max_train_id = min(test_ids) - 1
        if max_train_id < 1:  # need at least 2 train folds for a meaningful grid search
            continue

        train_ids = tuple(range(max_train_id + 1))
        train_start = folds[0].start
        train_end_raw = folds[max_train_id].end
        train_end = train_end_raw - timedelta(days=purge_days)
        if train_end <= train_start:
            continue

        # If test folds are NOT contiguous, we'd want to skip "test gap" days as
        # well. With ordering constraint + contiguous test, this rarely matters.
        test_start = folds[test_ids[0]].start
        test_end = folds[test_ids[-1]].end
        if test_ids != tuple(range(test_ids[0], test_ids[-1] + 1)):
            # Non-contiguous test: span the outer envelope; backtest engine
            # will run continuously across the inner gap. Acceptable for daily.
            pass

        splits.append(
            Split(
                split_id=len(splits),
                train_intervals=[(train_start, train_end)],
                test_start=test_start,
                test_end=test_end,
                test_fold_ids=test_ids,
                train_fold_ids=train_ids,
                purge_dropped_days=purge_days,
                embargo_dropped_days=embargo_days,
            )
        )
    return splits


def _run_one_split(
    split: Split,
    strategy_class: type,
    param_grid: dict[str, list[Any]],
    fixed_params: dict[str, Any],
    vt_symbol: str,
    interval: str,
    metric: str,
    min_trades: int,
    bt_kwargs: dict[str, Any],
    return_curves: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]] | None:
    """Run IS grid-search + OOS test for one Split. Returns row dict or None
    if the train window can't satisfy min_trades.

    If `return_curves=True`, returns (row, curves) where curves is
    `{'split_id', 'is_daily_df', 'oos_daily_df'}`. The IS curve requires re-
    running the winning param set with `return_daily_df=True` (one extra
    train backtest per split). Used by ensemble research to compute per-split
    inverse-vol weights and daily PnL combines.
    """
    # PWF: single train interval. For CPCV full, we'd concat multi-intervals,
    # but inner grid_search expects (start, end). Use the first (and only) one.
    train_start, train_end = split.train_intervals[0]

    try:
        best_params, is_stats, _ = grid_search(
            strategy_class=strategy_class,
            param_grid=param_grid,
            fixed_params=fixed_params,
            vt_symbol=vt_symbol,
            interval=interval,
            start=train_start,
            end=train_end,
            metric=metric,
            min_trades=min_trades,
            **bt_kwargs,
        )
    except RuntimeError as e:
        logger.warning("Split %d skipped (IS no params): %s", split.split_id, e)
        return None

    if return_curves:
        oos_stats, oos_daily = run_backtest(
            strategy_class=strategy_class,
            params=best_params,
            vt_symbol=vt_symbol,
            interval=interval,
            start=split.test_start,
            end=split.test_end,
            return_daily_df=True,
            **bt_kwargs,
        )
        _, is_daily = run_backtest(
            strategy_class=strategy_class,
            params=best_params,
            vt_symbol=vt_symbol,
            interval=interval,
            start=train_start,
            end=train_end,
            return_daily_df=True,
            **bt_kwargs,
        )
    else:
        oos_stats = run_backtest(
            strategy_class=strategy_class,
            params=best_params,
            vt_symbol=vt_symbol,
            interval=interval,
            start=split.test_start,
            end=split.test_end,
            **bt_kwargs,
        )

    row = {
        "split_id": split.split_id,
        "test_fold_ids": ",".join(str(i) for i in split.test_fold_ids),
        "train_start": train_start.date(),
        "train_end": train_end.date(),
        "test_start": split.test_start.date(),
        "test_end": split.test_end.date(),
        "purge_days": split.purge_dropped_days,
        "embargo_days": split.embargo_dropped_days,
        "best_params": {k: best_params[k] for k in param_grid},
        "is_sharpe": is_stats.get("sharpe_ratio"),
        "is_trades": is_stats.get("total_trade_count"),
        "oos_sharpe": oos_stats.get("sharpe_ratio"),
        "oos_return_pct": oos_stats.get("total_return"),
        "oos_max_dd_pct": oos_stats.get("max_ddpercent"),
        "oos_trades": oos_stats.get("total_trade_count"),
    }
    if return_curves:
        curves = {
            "split_id": split.split_id,
            "is_daily_df": is_daily,
            "oos_daily_df": oos_daily,
        }
        return row, curves
    return row


def run_pwf(
    strategy_class: type,
    param_grid: dict[str, list[Any]],
    fixed_params: dict[str, Any],
    vt_symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    n_folds: int = 10,
    purge_days: int = 20,
    metric: str = "sharpe_ratio",
    min_trades: int = 5,
    return_curves: bool = False,
    **bt_kwargs: Any,
):
    """Purged Walk-Forward. Drop-in for wfa.run_wfa with tighter leak control.

    If `return_curves=True`, returns (df, curves) where `curves` is a list of
    dicts (one per kept split) with keys `split_id`, `is_daily_df`,
    `oos_daily_df`. Mirrors wfa.run_wfa's return_curves contract so ensemble
    research can swap WFA ↔ PWF without changing downstream code.
    """
    folds = partition_into_folds(start, end, n_folds)
    splits = build_pwf_splits(folds, purge_days)
    logger.info("PWF: %d folds → %d splits (purge=%dd)", n_folds, len(splits), purge_days)

    rows: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    for sp in splits:
        logger.info(
            "Split %d: train %s→%s | test %s→%s",
            sp.split_id,
            sp.train_intervals[0][0].date(),
            sp.train_intervals[0][1].date(),
            sp.test_start.date(),
            sp.test_end.date(),
        )
        result = _run_one_split(
            sp,
            strategy_class,
            param_grid,
            fixed_params,
            vt_symbol,
            interval,
            metric,
            min_trades,
            bt_kwargs,
            return_curves=return_curves,
        )
        if result is None:
            continue
        if return_curves:
            assert isinstance(result, tuple)
            row, curve = result
            rows.append(row)
            curves.append(curve)
        else:
            assert isinstance(result, dict)
            rows.append(result)

    df = pd.DataFrame(rows)
    if return_curves:
        return df, curves
    return df


def run_cpcv_full(
    strategy_class: type,
    param_grid: dict[str, list[Any]],
    fixed_params: dict[str, Any],
    vt_symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    n_folds: int = 10,
    k_test: int = 1,
    purge_days: int = 20,
    embargo_days: int = 20,
    metric: str = "sharpe_ratio",
    min_trades: int = 5,
    **bt_kwargs: Any,
) -> pd.DataFrame:
    """Full CPCV with k_test test folds per split (under train-before-test
    ordering). k_test=1 is equivalent to run_pwf but goes through the more
    general code path."""
    folds = partition_into_folds(start, end, n_folds)
    splits = build_cpcv_splits(folds, k_test, purge_days, embargo_days)
    logger.info(
        "CPCV: %d folds × k_test=%d → %d splits (purge=%dd, embargo=%dd)",
        n_folds,
        k_test,
        len(splits),
        purge_days,
        embargo_days,
    )

    rows = []
    for sp in splits:
        row = _run_one_split(
            sp,
            strategy_class,
            param_grid,
            fixed_params,
            vt_symbol,
            interval,
            metric,
            min_trades,
            bt_kwargs,
        )
        if row is not None:
            rows.append(row)
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame, label: str = "") -> dict[str, Any]:
    """Distribution stats on OOS Sharpe across splits. The deliverable of CPCV
    is NOT a point estimate but this whole distribution — wide std means the
    parameter selector is fragile; tight + positive means the strategy
    generalises."""
    oos = df["oos_sharpe"].dropna()
    if oos.empty:
        return {"label": label, "n_splits": 0}
    return {
        "label": label,
        "n_splits": int(len(oos)),
        "oos_sharpe_mean": float(oos.mean()),
        "oos_sharpe_median": float(oos.median()),
        "oos_sharpe_std": float(oos.std()),
        "oos_sharpe_q10": float(oos.quantile(0.10)),
        "oos_sharpe_q25": float(oos.quantile(0.25)),
        "oos_sharpe_q75": float(oos.quantile(0.75)),
        "oos_sharpe_q90": float(oos.quantile(0.90)),
        "oos_sharpe_min": float(oos.min()),
        "oos_sharpe_max": float(oos.max()),
        "oos_positive_pct": float((oos > 0).mean() * 100),
        "is_oos_corr": float(df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1])
        if "is_sharpe" in df.columns
        else float("nan"),
    }


def print_summary(stats: dict[str, Any]) -> None:
    """Human-readable summary block."""
    if stats.get("n_splits", 0) == 0:
        print(f"  [{stats.get('label', '?')}] no splits produced")
        return
    print(f"  [{stats['label']}] n_splits={stats['n_splits']}")
    print(
        f"    OOS Sharpe:  mean={stats['oos_sharpe_mean']:+.3f}  "
        f"median={stats['oos_sharpe_median']:+.3f}  "
        f"std={stats['oos_sharpe_std']:.3f}"
    )
    print(
        f"    Quantiles:   q10={stats['oos_sharpe_q10']:+.3f}  "
        f"q25={stats['oos_sharpe_q25']:+.3f}  "
        f"q75={stats['oos_sharpe_q75']:+.3f}  "
        f"q90={stats['oos_sharpe_q90']:+.3f}"
    )
    print(f"    Range:       [{stats['oos_sharpe_min']:+.3f}, " f"{stats['oos_sharpe_max']:+.3f}]")
    print(f"    Positive %:  {stats['oos_positive_pct']:.1f}%")
    print(f"    IS-OOS corr: {stats['is_oos_corr']:+.3f}")


def main() -> int:
    """Smoke: run PWF on DoubleMa over AG daily, compare with wfa baseline."""
    from strategies.double_ma_strategy import DoubleMaStrategy

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.WARNING)

    df = run_pwf(
        strategy_class=DoubleMaStrategy,
        param_grid={"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]},
        fixed_params={"fixed_size": 1},
        vt_symbol="ag_continuous_adj15.SHFE",
        interval="1d",
        start=datetime(2012, 5, 10),
        end=datetime(2026, 5, 15),
        n_folds=10,
        purge_days=20,
        metric="sharpe_ratio",
        min_trades=5,
        capital=1_000_000,
        rate=5e-5,
        slippage=1,
        size=15,
        pricetick=1,
    )

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print(f"\n{'=' * 100}\nPWF SMOKE: DoubleMa/AG.adj15 daily, n_folds=10, purge=20d\n{'=' * 100}")
    print(df.to_string(index=False))

    stats = summarize(df, label="DoubleMa/AG PWF")
    print(f"\n{'=' * 100}\nSUMMARY\n{'=' * 100}")
    print_summary(stats)

    return 0


if __name__ == "__main__":
    sys.exit(main())
