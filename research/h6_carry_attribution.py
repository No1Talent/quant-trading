"""H6a: Carry attribution on I.raw daily PnL.

H5 surfaced a surprising finding: I's +0.445 raw Sharpe collapses to
+0.048 under ratio back-adjustment despite a positive IS-OOS Sharpe
correlation (+0.345). The hypothesis: I's "momentum" edge is actually
the carry premium leaking into close-to-close returns via rollover gaps.
Adjustment removes the gap → removes the alpha.

This script tests the hypothesis directly. Re-runs I.raw DoubleMa WFA
with curve capture, then attributes daily PnL by trading-day distance
to the nearest H1.5 OI-flagged rollover. If carry drives the signal,
PnL should concentrate around rollover events; if it's genuine momentum,
PnL should distribute roughly uniformly across the calendar.

Verdict thresholds (share of total PnL falling within ±5 trading days
of a rollover):
  > 50%        → [CARRY_CONFIRMED]
  20-50%       → [CARRY_PARTIAL]
  ~uniform     → [CARRY_REJECTED]
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]

import pandas as pd  # noqa: E402
from vnpy.trader.constant import Exchange, Interval  # noqa: E402

from research.h1_5_calendar_rollover import (  # noqa: E402
    GAP_FLOOR_PCT,
    OI_PCT_THRESHOLD,
    detect_rollovers_oi,
    load_bars_to_df,
)

VT_SYMBOL = "i_continuous.DCE"
START = datetime(2013, 10, 18)
END = datetime(2026, 5, 15)
BT: dict[str, Any] = dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=100, pricetick=0.5)
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
MIN_TRADES = 5

# Bucket definitions (trading-day distance to nearest rollover day)
BUCKETS: list[tuple[str, int, int]] = [
    ("ON_ROLL", 0, 0),
    ("ROLL_pm1", 1, 1),
    ("ROLL_pm2to5", 2, 5),
    ("ROLL_pm6to10", 6, 10),
    ("FAR_gt10", 11, 10**9),
]


def daily_pnl_from_curves(curves: list[dict]) -> pd.Series:
    """Concatenate OOS net_pnl across folds, unscaled."""
    pieces: list[pd.Series] = []
    for c in curves:
        df = c.get("oos_daily_df")
        if df is None or len(df) == 0:
            continue
        s = df["net_pnl"].copy()
        s.index = pd.to_datetime(s.index)
        pieces.append(s)
    if not pieces:
        return pd.Series(dtype=float)
    out = pd.concat(pieces).sort_index()
    # Folds are non-overlapping by construction (step==test), but guard
    # against accidental duplicates.
    out = out.groupby(out.index).sum()
    return out


def trading_day_distance_to_rollovers(
    pnl_dates: pd.DatetimeIndex,
    rollover_dates: pd.DatetimeIndex,
    all_bar_dates: pd.DatetimeIndex,
) -> pd.Series:
    """Distance in trading days (bar-index) from each pnl_date to its
    nearest rollover_date. Uses the full bar-date index so weekends/
    holidays don't inflate distance."""
    # Map each calendar date in all_bar_dates → trading-day index
    bar_pos = pd.Series(range(len(all_bar_dates)), index=all_bar_dates)
    # Rollover trading-day positions
    roll_pos = bar_pos.reindex(rollover_dates).dropna().astype(int).values
    if len(roll_pos) == 0:
        return pd.Series([10**9] * len(pnl_dates), index=pnl_dates)
    # PnL date trading-day positions
    pnl_pos = bar_pos.reindex(pnl_dates).astype(float)
    # For dates not in the bar index (shouldn't happen), leave NaN → handled
    dists = []
    roll_sorted = np.sort(roll_pos)
    for p in pnl_pos.values:
        if not np.isfinite(p):
            dists.append(10**9)
            continue
        # Binary-search nearest
        idx = np.searchsorted(roll_sorted, p)
        candidates = []
        if idx < len(roll_sorted):
            candidates.append(abs(roll_sorted[idx] - p))
        if idx > 0:
            candidates.append(abs(roll_sorted[idx - 1] - p))
        dists.append(int(min(candidates)))
    return pd.Series(dists, index=pnl_dates, name="days_since_rollover")


def bucket_for(dist: int) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= dist <= hi:
            return name
    return "OUT_OF_BAND"


def summarize_buckets(df: pd.DataFrame) -> pd.DataFrame:
    """One row per bucket: counts, PnL aggregates, Sharpe."""
    total_pnl = float(df["net_pnl"].sum())
    total_days = len(df)
    rows = []
    for name, _lo, _hi in BUCKETS:
        sub = df[df["bucket"] == name]
        n = len(sub)
        pnl_sum = float(sub["net_pnl"].sum())
        if n > 0:
            pnl_mean = float(sub["net_pnl"].mean())
            pnl_std = float(sub["net_pnl"].std())
            sharpe = (pnl_mean / pnl_std) * np.sqrt(252) if pnl_std > 0 else float("nan")
        else:
            pnl_mean = float("nan")
            pnl_std = float("nan")
            sharpe = float("nan")
        rows.append(
            {
                "bucket": name,
                "n_days": n,
                "pnl_sum": pnl_sum,
                "pnl_mean_daily": pnl_mean,
                "pnl_std_daily": pnl_std,
                "share_of_total_pnl": pnl_sum / total_pnl if total_pnl != 0 else float("nan"),
                "share_of_total_days": n / total_days if total_days > 0 else float("nan"),
                "bucket_sharpe_ann": sharpe,
            }
        )
    return pd.DataFrame(rows)


def verdict_from_summary(summary: pd.DataFrame) -> str:
    """Apply H6a carry-confirmed / partial / rejected rule."""
    rolladj = summary[summary["bucket"].isin(["ON_ROLL", "ROLL_pm1", "ROLL_pm2to5"])]
    far = summary[summary["bucket"] == "FAR_gt10"]
    if len(rolladj) == 0 or len(far) == 0:
        return "[UNCLASSIFIED] missing bucket rows"

    pnl_share_near = float(rolladj["share_of_total_pnl"].sum())
    day_share_near = float(rolladj["share_of_total_days"].sum())
    concentration = pnl_share_near / day_share_near if day_share_near > 0 else float("nan")

    msg_head = (
        f"PnL share within ±5 trading days of rollover: {pnl_share_near:.1%}\n"
        f"Day share within ±5 trading days of rollover: {day_share_near:.1%}\n"
        f"Concentration ratio (pnl_share / day_share): {concentration:.2f}x\n"
    )

    if pnl_share_near > 0.50 and day_share_near < 0.15:
        return (
            msg_head + "\n[CARRY_CONFIRMED] >50% of total PnL falls within ±5 trading\n"
            "  days of rollovers despite those days being <15% of the calendar.\n"
            "  I.raw's edge IS the carry premium, captured via rollover-day price gaps.\n"
            "  → Unlocks H6b: build a clean carry-overlay strategy on I."
        )
    if pnl_share_near > 0.20:
        return (
            msg_head + "\n[CARRY_PARTIAL] 20-50% of PnL is rollover-adjacent. Mixed source\n"
            "  signal: a carry component AND a trend component. H6b becomes a stacked\n"
            "  carry+momentum strategy; harder to formalise cleanly."
        )
    return (
        msg_head + "\n[CARRY_REJECTED] PnL distribution roughly tracks day-share. I's edge\n"
        "  is NOT primarily carry — adjustment-driven Sharpe collapse must have another\n"
        "  cause (e.g. adjustment shifts trade timing past slow MA, missing trend onset).\n"
        "  Investigate adjustment-induced trade-timing effects next."
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 80}\n# H6a: carry attribution on I.raw\n{'#' * 80}")
    print(f"  Symbol: {VT_SYMBOL}  {START.date()} → {END.date()}")
    print("  Hypothesis: I's +0.445 Sharpe lives at rollover events (carry)")

    # Phase 1: WFA with curve capture.
    print("\n--- Phase 1: I.raw WFA with curve capture ---")
    from research.wfa import run_wfa
    from strategies.double_ma_strategy import DoubleMaStrategy

    df_wfa, curves = run_wfa(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": 1},
        vt_symbol=VT_SYMBOL,
        interval="1d",
        start=START,
        end=END,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        return_curves=True,
        **BT,
    )
    oos = df_wfa["oos_sharpe"].dropna()
    print(
        f"  WFA: {len(df_wfa)} folds, OOS Sharpe mean={oos.mean():+.3f}, "
        f"pos%={(oos > 0).mean() * 100:.1f}, total return%={df_wfa['oos_return_pct'].sum():+.2f}"
    )

    # Phase 2: concatenate OOS daily PnL + sanity-check vs WFA total.
    print("\n--- Phase 2: OOS daily PnL aggregation ---")
    pnl_series = daily_pnl_from_curves(curves)
    if len(pnl_series) == 0:
        print("ERROR: no OOS daily PnL captured — check curve capture path")
        return 1
    total_pnl = float(pnl_series.sum())
    print(
        f"  Reconstructed OOS days: {len(pnl_series)}, "
        f"total net_pnl: {total_pnl:,.0f} ({total_pnl / BT['capital']:+.2%} of capital)"
    )
    print(
        f"  Compare to WFA total_return: {df_wfa['oos_return_pct'].sum():+.2f}% "
        f"(sanity: capture-mode total should match the no-capture run; expect ~+7.92%)"
    )

    # Phase 3: rollover dates from H1.5 OI mask.
    print("\n--- Phase 3: H1.5 OI rollover mask on i_continuous ---")
    i_bars = load_bars_to_df("i_continuous", Exchange.DCE, Interval.DAILY)
    mask = detect_rollovers_oi(i_bars, OI_PCT_THRESHOLD, GAP_FLOOR_PCT)
    bar_dates = pd.to_datetime(i_bars["datetime"]).dt.normalize()
    rollover_dates = pd.DatetimeIndex(bar_dates[mask].values)
    print(f"  Total bars: {len(i_bars)}, rollover days flagged: {len(rollover_dates)}")

    # Phase 4: assign distance + bucket per PnL day.
    print("\n--- Phase 4: bucketing by trading-day distance to rollover ---")
    pnl_dates = pd.DatetimeIndex(pnl_series.index).normalize()
    pnl_series.index = pnl_dates
    dist = trading_day_distance_to_rollovers(
        pnl_dates, rollover_dates, pd.DatetimeIndex(bar_dates.values)
    )
    daily_df = pd.DataFrame(
        {
            "date": pnl_dates,
            "net_pnl": pnl_series.values,
            "days_since_rollover": dist.values,
        }
    )
    daily_df["bucket"] = daily_df["days_since_rollover"].apply(bucket_for)

    # Sanity checks
    assert len(daily_df) == len(pnl_series), "row count mismatch"
    assert abs(float(daily_df["net_pnl"].sum()) - total_pnl) < 1e-6, "PnL sum mismatch"
    bucket_counts = daily_df["bucket"].value_counts().sum()
    assert bucket_counts == len(daily_df), "bucket assignment lost rows"

    # Phase 5: segment table + verdict.
    print("\n--- Phase 5: segment summary ---")
    summary = summarize_buckets(daily_df)
    print()
    print(
        f"  {'Bucket':14s} {'days':>6s} {'pnl_sum':>12s} {'mean/d':>10s} "
        f"{'std/d':>10s} {'pnl_share':>10s} {'day_share':>10s} {'Sharpe_ann':>11s}"
    )
    print("  " + "-" * 95)
    for _, r in summary.iterrows():
        print(
            f"  {r['bucket']:14s} {r['n_days']:>6d} {r['pnl_sum']:>+12,.0f} "
            f"{r['pnl_mean_daily']:>+10.1f} {r['pnl_std_daily']:>10.1f} "
            f"{r['share_of_total_pnl']:>+10.1%} {r['share_of_total_days']:>10.1%} "
            f"{r['bucket_sharpe_ann']:>+11.3f}"
        )

    print(f"\n{'=' * 80}\nH6a VERDICT\n{'=' * 80}")
    print(verdict_from_summary(summary))

    # Phase 6: save artefacts.
    print("\n--- Phase 6: save artefacts ---")
    out_dir = REPO_ROOT / "research"
    daily_path = out_dir / "h6_carry_attribution.csv"
    summary_path = out_dir / "h6_segment_summary.csv"
    daily_df.to_csv(daily_path, index=False)
    summary.to_csv(summary_path, index=False)
    print(f"\nDaily attribution  → {daily_path}")
    print(f"Segment summary    → {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
