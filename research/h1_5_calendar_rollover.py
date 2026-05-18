"""H1.5: surgical rollover detection using open-interest + gap confirmation.

H1's crude |gap|>1.5% detector flagged 257 days as rollovers across 14 years.
For AG (silver, 6 deliveries/yr), the expected count is ~84. Profile of the
ag_continuous bars shows why the gap-only detector was wrong:

- |gap|>1.0% days are uniform across months (30-54 per month) — dominated by
  macro news jumps (NFP/CPI), not contract switches.
- |OI pct change|>20% gives 81 days — almost exactly matching expectation.
- Those high-OI days DO cluster in odd months (m=1:5, m=5:11, m=7:3, m=9:3,
  m=11:5 of the 43 OI>30% days), confirming the textbook "roll happens in
  non-delivery months" pattern is real but invisible in the noisy gap signal.

H1.5 detector: |ΔOI| > OI_PCT_THRESHOLD AND |gap| > GAP_FLOOR_PCT.
- OI jump is the primary signal (signature of contract-switch in main-cont).
- Gap floor filters away OI churns that didn't produce a tradeable price step.

Calendar fallback is also provided in case open_interest is missing or zero
on this dataset (we verified it is populated for ag_continuous).
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

# Primary detector thresholds (chosen from offline profile of ag_continuous)
OI_PCT_THRESHOLD = 20.0  # %; ag_continuous: 81 days match, near 14yr*6/yr=84 expected
GAP_FLOOR_PCT = 0.3  # %; weed out OI-only churns with no price step

# Calendar fallback (used only if OI column is empty/zero)
ROLLOVER_MONTHS = {1, 3, 5, 7, 9, 11}  # AG rolls in odd months (delivery is even)
ROLLOVER_DAYS = range(10, 26)  # 10..25 of month
CAL_GAP_THRESHOLD_PCT = 1.0


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


def detect_rollovers_oi(
    df: pd.DataFrame,
    oi_threshold_pct: float = OI_PCT_THRESHOLD,
    gap_floor_pct: float = GAP_FLOOR_PCT,
) -> pd.Series:
    """Rollover = large open-interest step + non-trivial price gap.

    OI step indicates main-contract switch; gap floor confirms the splice
    produced an adjustable price jump (skips zero-gap OI churns).
    """
    prev_close = df["close"].shift(1)
    gap_abs_pct = ((df["open"] - prev_close) / prev_close * 100).abs()

    oi_change_pct = df["open_interest"].pct_change() * 100
    oi_abs_pct = oi_change_pct.abs()

    mask = (oi_abs_pct > oi_threshold_pct) & (gap_abs_pct > gap_floor_pct)
    return mask.fillna(False)


def detect_rollovers_calendar(
    df: pd.DataFrame, gap_threshold_pct: float = CAL_GAP_THRESHOLD_PCT
) -> pd.Series:
    """Fallback when OI is unavailable: odd-month days 10-25 with |gap| > 1.0%."""
    dt = pd.to_datetime(df["datetime"])
    in_window = dt.dt.month.isin(ROLLOVER_MONTHS) & dt.dt.day.isin(list(ROLLOVER_DAYS))

    prev_close = df["close"].shift(1)
    gap_abs_pct = ((df["open"] - prev_close) / prev_close * 100).abs()

    return (in_window & (gap_abs_pct > gap_threshold_pct)).fillna(False)


def back_adjust(df: pd.DataFrame, rollover_mask: pd.Series) -> pd.DataFrame:
    """Shift prior bars by accumulated rollover gaps (same algorithm as H1)."""
    df = df.copy().reset_index(drop=True)
    prev_close = df["close"].shift(1)
    gap = df["open"] - prev_close
    gap_at_rollovers = gap.where(rollover_mask, 0.0)

    rev_cumsum = gap_at_rollovers[::-1].cumsum()[::-1]
    cum_adj = rev_cumsum - gap_at_rollovers

    for col in ["open", "high", "low", "close"]:
        df[col] = df[col] + cum_adj

    return df


def diagnose(df: pd.DataFrame, mask: pd.Series, label: str) -> None:
    n = int(mask.sum())
    print(f"\n{label}:")
    print(f"  Days flagged: {n}")
    print("  Reference: AG ~84 expected over 14 years (6 deliveries/yr)")

    if n == 0:
        return

    prev_close = df["close"].shift(1)
    gap_pct = (df["open"] - prev_close) / prev_close * 100
    rg = gap_pct[mask]
    print(
        f"  Gap distribution: mean={rg.mean():+.2f}% std={rg.std():.2f}% "
        f"min={rg.min():+.2f}% max={rg.max():+.2f}%"
    )

    dts = pd.to_datetime(df.loc[mask, "datetime"])
    by_month = dts.dt.month.value_counts().sort_index()
    print("  Monthly distribution (AG rolls in odd months 1,3,5,7,9,11):")
    for m, c in by_month.items():
        marker = "*" if m in ROLLOVER_MONTHS else " "
        print(f"    {marker}{m:2d}: {c}")
    odd_share = sum(c for m, c in by_month.items() if m in ROLLOVER_MONTHS) / n * 100
    print(f"  Odd-month share: {odd_share:.1f}% (random ~50%, real rolls expect ~80%+)")


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

    print(f"\n{'#' * 80}\n# H1.5: Surgical Rollover Detection (OI-jump + gap-confirm)\n{'#' * 80}")

    # Phase 1: load original ag_continuous, run BOTH detectors for comparison
    print("\n=== Phase 1: detecting rollover days ===")
    df_orig = load_bars_to_df("ag_continuous", Exchange.SHFE, Interval.DAILY)

    has_oi = (df_orig["open_interest"].fillna(0) > 0).any()
    print(f"  open_interest present: {has_oi}")

    # Crude H1 detector for side-by-side comparison
    prev_close = df_orig["close"].shift(1)
    gap_abs_pct = ((df_orig["open"] - prev_close) / prev_close * 100).abs()
    crude_mask = (gap_abs_pct > 1.5).fillna(False)
    diagnose(df_orig, crude_mask, "H1 baseline detector (|gap|>1.5%, crude)")

    if has_oi:
        precise_mask = detect_rollovers_oi(df_orig, OI_PCT_THRESHOLD, GAP_FLOOR_PCT)
        diagnose(
            df_orig,
            precise_mask,
            f"H1.5 OI-based detector (|ΔOI|>{OI_PCT_THRESHOLD}% AND |gap|>{GAP_FLOOR_PCT}%)",
        )
    else:
        precise_mask = detect_rollovers_calendar(df_orig, CAL_GAP_THRESHOLD_PCT)
        diagnose(
            df_orig,
            precise_mask,
            f"H1.5 calendar-fallback (odd months 10-25, |gap|>{CAL_GAP_THRESHOLD_PCT}%)",
        )

    # Phase 2: back-adjust and export
    print("\n=== Phase 2: back-adjusting and importing ===")
    df_adj = back_adjust(df_orig, precise_mask)
    post_mask = (
        detect_rollovers_oi(df_adj, OI_PCT_THRESHOLD, GAP_FLOOR_PCT)
        if has_oi
        else (detect_rollovers_calendar(df_adj, CAL_GAP_THRESHOLD_PCT))
    )
    print(f"  Post-adjustment rollovers remaining (should be ~0): {int(post_mask.sum())}")

    new_symbol = "ag_continuous_adj15"
    export_adjusted(df_adj, new_symbol, Exchange.SHFE)

    # Phase 3: re-run WFA on precisely-adjusted symbol
    print(f"\n=== Phase 3: re-running DoubleMa/AG WFA on {new_symbol} ===")
    adj_result = rerun_doublema_ag_wfa(new_symbol)

    # Phase 4: compare against (a) baseline raw continuous and (b) H1 crude-adjusted
    print(f"\n{'=' * 84}\n=== Phase 4: BASELINE vs H1-CRUDE vs H1.5-PRECISE ===\n{'=' * 84}")

    baseline = {
        "folds": 17,
        "oos_sharpe_mean": 0.344,
        "oos_sharpe_median": 0.127,
        "oos_positive_pct": 64.7,
        "total_oos_return_pct": 7.456,
        "is_oos_corr": 0.105,
    }
    h1_crude = {
        "folds": 17,
        "oos_sharpe_mean": 0.163,
        "oos_sharpe_median": 0.041,
        "oos_positive_pct": 47.1,
        "total_oos_return_pct": 4.391,
        "is_oos_corr": -0.291,
    }

    rows = [
        ("Folds", adj_result["folds"], h1_crude["folds"], baseline["folds"]),
        (
            "OOS Sharpe mean",
            adj_result["oos_sharpe_mean"],
            h1_crude["oos_sharpe_mean"],
            baseline["oos_sharpe_mean"],
        ),
        (
            "OOS Sharpe median",
            adj_result["oos_sharpe_median"],
            h1_crude["oos_sharpe_median"],
            baseline["oos_sharpe_median"],
        ),
        (
            "OOS positive %",
            adj_result["oos_positive_pct"],
            h1_crude["oos_positive_pct"],
            baseline["oos_positive_pct"],
        ),
        (
            "Total OOS return %",
            adj_result["total_oos_return_pct"],
            h1_crude["total_oos_return_pct"],
            baseline["total_oos_return_pct"],
        ),
        (
            "IS-OOS corr",
            adj_result["is_oos_corr"],
            h1_crude["is_oos_corr"],
            baseline["is_oos_corr"],
        ),
    ]
    print(f"  {'Metric':22s} {'H1.5 (precise)':>16s} {'H1 (crude)':>14s} {'Baseline':>12s}")
    print("  " + "-" * 68)
    for name, v15, v1, vb in rows:
        print(f"  {name:22s} {v15:>+16.3f} {v1:>+14.3f} {vb:>+12.3f}")

    print(f"\n{'=' * 84}\nVERDICT\n{'=' * 84}")
    s = adj_result["oos_sharpe_mean"]
    corr = adj_result["is_oos_corr"]
    print(f"  H1.5 precise back-adjust: OOS Sharpe={s:+.3f}, IS-OOS corr={corr:+.3f}")
    if s > 0.25 and corr > 0.05:
        print("  [ALPHA SURVIVES] Sharpe held and correlation stayed positive after")
        print("                   surgical rollover removal. Real signal.")
        print("                   Next: H2 cross-instrument validation (hc/i/au/cu).")
    elif s > 0.20:
        print("  [LIKELY REAL] Sharpe held above +0.20 after surgical adjustment.")
        print("                The H1 crude detector was indeed over-scrubbing.")
        if corr <= 0.05:
            print(f"                IS-OOS corr {corr:+.3f} still weak — selection is")
            print(
                "                noisier than the signal. Consider ensemble before paper trading."
            )
    elif s > 0.10:
        print("  [MARGINAL] Sharpe in +0.10..+0.20 band — partial gap contribution.")
        print("             Alpha exists but is small; cost/slippage realism is critical.")
    else:
        print(f"  [WEAK] Sharpe collapsed to {s:+.3f}. Even with surgical removal the")
        print("         signal is largely rollover-driven. Daily-continuous discredited.")

    out_path = REPO_ROOT / "research" / "wfa_results_h1_5_precise_rollover.csv"
    adj_result["df"].to_csv(out_path, index=False)
    print(f"\nFull adjusted fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
