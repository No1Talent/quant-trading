"""H1: quantify how much of DoubleMa/AG daily's +0.344 OOS Sharpe is rollover artifact.

Main-continuous data splices different actual contracts month-by-month. On the
splice day, close-to-open prices jump by the basis (real but un-tradeable —
in real trading you'd close the old contract and open the new at market, not
realize the gap as P&L).

Approach: back-adjust the continuous series. Detect each rollover day by
|open[t] - close[t-1]| / close[t-1] > threshold. Then shift all bars BEFORE
the rollover up/down by that gap, making the series continuous (no jumps).
Save as ag_continuous_adj.SHFE and re-run the same DoubleMa/AG WFA.

If +0.344 → ~-0.1, the alpha was rollover-fueled (un-tradeable).
If +0.344 → +0.2 or better, the alpha survives gap removal — real signal.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]

import pandas as pd  # noqa: E402
from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.database import get_database  # noqa: E402

GAP_THRESHOLD_PCT = 1.5


def load_bars_to_df(symbol: str, exchange: Exchange, interval: Interval) -> pd.DataFrame:
    db = get_database()
    bars = db.load_bar_data(symbol, exchange, interval, datetime(2000, 1, 1), datetime(2030, 1, 1))
    rows = []
    for b in bars:
        rows.append(
            {
                "datetime": b.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "open": b.open_price,
                "high": b.high_price,
                "low": b.low_price,
                "close": b.close_price,
                "volume": b.volume,
                "open_interest": b.open_interest,
            }
        )
    return pd.DataFrame(rows)


def detect_rollovers(df: pd.DataFrame, threshold_pct: float = 1.5) -> pd.Series:
    prev_close = df["close"].shift(1)
    gap_pct = ((df["open"] - prev_close) / prev_close * 100).abs()
    return (gap_pct > threshold_pct).fillna(False)


def back_adjust(df: pd.DataFrame, rollover_mask: pd.Series) -> pd.DataFrame:
    """Shift prior bars up/down by each rollover gap to eliminate splice jumps.

    For each rollover at index t with gap = open[t] - close[t-1]:
      add `gap` to OHLC of all bars strictly before t.
    """
    df = df.copy().reset_index(drop=True)
    prev_close = df["close"].shift(1)
    gap = df["open"] - prev_close
    gap_at_rollovers = gap.where(rollover_mask, 0.0)

    # For each bar i, cumulative adjustment = sum of gaps at rollovers t > i
    rev_cumsum = gap_at_rollovers[::-1].cumsum()[::-1]
    cum_adj = rev_cumsum - gap_at_rollovers  # exclude self

    for col in ["open", "high", "low", "close"]:
        df[col] = df[col] + cum_adj

    return df


def diagnose_rollovers(df: pd.DataFrame, rollover_mask: pd.Series, label: str) -> None:
    n_rollovers = int(rollover_mask.sum())
    n_total = len(df)
    print(f"\n{label}:")
    print(f"  Total bars: {n_total}")
    print(f"  Rollover candidates (|gap| > {GAP_THRESHOLD_PCT}%): {n_rollovers}")
    print(f"  Rollover rate: {n_rollovers/n_total*100:.2f}% of bars")

    if n_rollovers == 0:
        return

    prev_close = df["close"].shift(1)
    gap_pct = (df["open"] - prev_close) / prev_close * 100
    rollover_gaps = gap_pct[rollover_mask]
    print(
        f"  Gap distribution (%): mean={rollover_gaps.mean():+.2f}  "
        f"std={rollover_gaps.std():.2f}  min={rollover_gaps.min():+.2f}  "
        f"max={rollover_gaps.max():+.2f}"
    )

    # Monthly distribution to sanity-check it's near silver delivery months (6, 12)
    rollover_dts = pd.to_datetime(df.loc[rollover_mask, "datetime"])
    print("  Monthly distribution of rollovers (silver delivery: 2,4,6,8,10,12):")
    for month, count in rollover_dts.dt.month.value_counts().sort_index().items():
        print(f"    {month:2d}: {count}")


def export_adjusted(df_adj: pd.DataFrame, new_symbol: str, exchange: Exchange) -> None:
    csv_dir = REPO_ROOT / "data" / "bar"
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"{new_symbol}_daily.csv"
    df_adj.to_csv(csv_path, index=False)

    from import_data import import_csv_to_database

    import_csv_to_database(
        csv_path=csv_path,
        symbol=new_symbol,
        exchange=exchange,
        interval=Interval.DAILY,
        batch_size=5000,
        resume=False,
    )


def rerun_doublema_ag_wfa(symbol: str) -> dict:
    """Re-run the exact DoubleMa/AG WFA on the given symbol."""
    from research.wfa_daily_continuous import AG_BT, DM_GRID, STEP_DAYS, TEST_DAYS, TRAIN_DAYS
    from research.wfa_rb_batch import run_batch
    from strategies.double_ma_strategy import DoubleMaStrategy

    contracts = [(f"{symbol}.SHFE", datetime(2012, 5, 10), datetime(2026, 5, 15))]
    df = run_batch(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": 1},
        label=f"DoubleMa/{symbol}",
        contracts=contracts,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        bt_kwargs=AG_BT,
        interval="1d",
        min_trades=5,
    )

    oos = df["oos_sharpe"].dropna()
    return {
        "df": df,
        "folds": len(df),
        "oos_sharpe_mean": float(oos.mean()),
        "oos_sharpe_median": float(oos.median()),
        "oos_positive_pct": float((oos > 0).mean() * 100),
        "total_oos_return_pct": float(df["oos_return_pct"].sum()),
        "is_oos_corr": float(df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]),
        "oos_sharpe_min": float(oos.min()),
        "oos_sharpe_max": float(oos.max()),
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(
        f"\n{'#' * 80}\n# H1: Rollover-Gap Sensitivity (DoubleMa/AG daily continuous)\n{'#' * 80}"
    )

    # Phase 1: load original ag_continuous, detect rollovers
    print("\n=== Phase 1: detecting rollover days ===")
    df_orig = load_bars_to_df("ag_continuous", Exchange.SHFE, Interval.DAILY)
    rollover_mask = detect_rollovers(df_orig, GAP_THRESHOLD_PCT)
    diagnose_rollovers(df_orig, rollover_mask, "ag_continuous (original)")

    # Phase 2: back-adjust and save
    print("\n=== Phase 2: back-adjusting and importing ===")
    df_adj = back_adjust(df_orig, rollover_mask)

    # Sanity: post-adjustment, large gaps should be gone
    post_mask = detect_rollovers(df_adj, GAP_THRESHOLD_PCT)
    print(
        f"  Post-adjustment rollover-sized gaps remaining: {int(post_mask.sum())} "
        f"(should be ~0; non-zero means consecutive rollovers within same bar)"
    )

    new_symbol = "ag_continuous_adj"
    export_adjusted(df_adj, new_symbol, Exchange.SHFE)

    # Phase 3: re-run WFA on adjusted symbol
    print(f"\n=== Phase 3: re-running DoubleMa/AG WFA on {new_symbol} ===")
    adj_result = rerun_doublema_ag_wfa(new_symbol)

    # Phase 4: compare
    print(f"\n{'=' * 80}\n=== Phase 4: ORIGINAL vs ROLLOVER-ADJUSTED ===\n{'=' * 80}")

    baseline = {
        "folds": 17,
        "oos_sharpe_mean": 0.344,
        "oos_sharpe_median": 0.127,
        "oos_positive_pct": 64.7,
        "total_oos_return_pct": 7.456,
        "is_oos_corr": 0.105,
        "oos_sharpe_min": -0.845,
        "oos_sharpe_max": 1.726,
    }

    rows = [
        ("Folds", adj_result["folds"], baseline["folds"]),
        ("OOS Sharpe mean", adj_result["oos_sharpe_mean"], baseline["oos_sharpe_mean"]),
        ("OOS Sharpe median", adj_result["oos_sharpe_median"], baseline["oos_sharpe_median"]),
        ("OOS positive %", adj_result["oos_positive_pct"], baseline["oos_positive_pct"]),
        (
            "Total OOS return %",
            adj_result["total_oos_return_pct"],
            baseline["total_oos_return_pct"],
        ),
        ("IS-OOS corr", adj_result["is_oos_corr"], baseline["is_oos_corr"]),
        ("OOS Sharpe min", adj_result["oos_sharpe_min"], baseline["oos_sharpe_min"]),
        ("OOS Sharpe max", adj_result["oos_sharpe_max"], baseline["oos_sharpe_max"]),
    ]
    print(f"  {'Metric':22s} {'Adjusted':>14s} {'Original':>14s} {'Δ':>12s}")
    print("  " + "-" * 66)
    for name, v_new, v_old in rows:
        delta = v_new - v_old
        print(f"  {name:22s} {v_new:>+14.3f} {v_old:>+14.3f} {delta:>+12.3f}")

    print(f"\n{'=' * 80}\nVERDICT\n{'=' * 80}")
    adj_sharpe = adj_result["oos_sharpe_mean"]
    if adj_sharpe > 0.2:
        print(f"  [REAL] OOS Sharpe held at {adj_sharpe:+.3f} after gap removal.")
        print("         The +0.344 was NOT a rollover artifact. Real tradeable alpha.")
        print("         Next: cost realism check, then SimNow paper trading.")
    elif adj_sharpe > 0:
        print(f"  [DEGRADED] OOS Sharpe dropped to {adj_sharpe:+.3f}. Partial gap contribution.")
        print("             Some alpha survives but smaller than original suggested.")
        print("             Worth careful cost/slippage study before paper trading.")
    else:
        print(f"  [ROLLOVER-FUELED] OOS Sharpe collapsed to {adj_sharpe:+.3f}.")
        print("                    The +0.344 was largely gap-driven, not tradeable alpha.")
        print(
            "                    Daily continuous results discredited; need rollover-aware data source."
        )

    out_path = REPO_ROOT / "research" / "wfa_results_h1_rollover_adjusted.csv"
    adj_result["df"].to_csv(out_path, index=False)
    print(f"\nFull adjusted fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
