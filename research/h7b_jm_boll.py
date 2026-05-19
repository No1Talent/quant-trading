"""H7b: Does Boll mean-reversion produce a tradeable edge on JM (焦煤)?

Why this follows H7. H7 ruled out DoubleMa daily on JM (OOS Sharpe -0.079,
31% positive folds — sub-random). That hit rate is suspicious: it suggests
JM is either mean-reverting OR that rollover gaps are forging fake DoubleMa
crosses in the raw continuous. Boll separates those hypotheses:
  - If Boll/adj15 makes money on JM, JM is genuinely mean-reverting and
    DoubleMa lost because it was on the wrong side of the regime.
  - If Boll fails on both raw and adj15, JM is just noisy at the daily
    horizon and we should leave it alone.

Why adj15 is the primary variant here (UNLIKE in H7). Boll's signal IS the
σ-channel — its core math is `mean ± dev × stdev`. Raw continuous has
rollover gaps that inflate stdev artificially (one ~5% rollover gap on a
700d window can move σ by ~0.2%, and the gap day itself often breaches the
band → fake entries). The H1.5 OI back-adjust collapses those gaps into
zero-return days, restoring statistical integrity of σ. For trend signals
(DoubleMa) the gap is just a cross artifact and either variant works;
for vol-channel signals (Boll), raw is mathematically unsound.

So: report both, but lead with adj15.

Grid choice. JM has higher realised vol than I — narrow dev (1.5) will whip;
extend the upper end to 2.5 and 3.0 per Gemini's recommendation so we can
see whether the edge wants wider channels. Window kept conservative: 15/20/30
spans roughly 3-6 trading weeks, matching JM's policy-driven swing horizon.

Decision rule (same shape as H7):
  Sharpe > +0.30 AND pos% > 60  → [PROMOTE]
  Sharpe > +0.15 AND pos% > 55  → [PARTIAL]
  Sharpe > 0                    → [WEAK]
  otherwise                     → [NO_EDGE]

Prerequisites: H7 must have run successfully — it populates jm_continuous +
jm_continuous_adj15 in the DB. This script will skip data ingestion entirely
and just run WFA on what's there. (Set FORCE_REFETCH=True if you want to
re-build the back-adjusted series — should not be needed.)

Run:
  python research/h7b_jm_boll.py
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

import numpy as np  # noqa: E402

if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]

import pandas as pd  # noqa: E402
from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.database import get_database  # noqa: E402

JM: dict[str, Any] = {
    "sym": "jm",
    "ak_symbol": "JM0",
    "exchange": Exchange.DCE,
    "start": datetime(2013, 4, 1),
    "bt": dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=60, pricetick=0.5),
}

END_DATE = datetime(2026, 5, 15)

TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
# boll_dev pushed to 3.0 — JM realised vol is high enough that 2.0 will whip;
# we want to see whether wider channels pay off before declaring no-edge.
BOLL_GRID: dict[str, list[Any]] = {
    "boll_window": [15, 20, 30],
    "boll_dev": [1.5, 2.0, 2.5, 3.0],
}
MIN_TRADES = 5

FORCE_REFETCH = False


def _db_has_symbol(symbol: str, exchange: Exchange, interval: Interval) -> bool:
    db = get_database()
    overviews = db.get_bar_overview()
    for ov in overviews:
        if ov.symbol == symbol and ov.exchange == exchange and ov.interval == interval:
            return ov.count > 0
    return False


def ensure_data() -> tuple[str, str]:
    raw_symbol = f"{JM['sym']}_continuous"
    adj_symbol = f"{JM['sym']}_continuous_adj15"

    raw_ready = _db_has_symbol(raw_symbol, JM["exchange"], Interval.DAILY)
    adj_ready = _db_has_symbol(adj_symbol, JM["exchange"], Interval.DAILY)

    if not (raw_ready and adj_ready) or FORCE_REFETCH:
        # Reuse H7's ingestion pipeline rather than duplicating it
        from research.h2_cross_instrument import (
            adjust_and_import,
            fetch_and_import_continuous,
        )

        if not raw_ready or FORCE_REFETCH:
            print(f"\n--- Fetching {JM['ak_symbol']} → {raw_symbol} ---")
            fetch_and_import_continuous(JM)
        if not adj_ready or FORCE_REFETCH:
            print(f"\n--- Building {adj_symbol} via H1.5 OI back-adjust ---")
            adjust_and_import(JM, raw_symbol)
    else:
        print("\n--- Both variants already in DB; running WFA only ---")

    return raw_symbol, adj_symbol


def run_wfa_variant(db_symbol: str, variant_label: str) -> dict[str, Any]:
    from research.wfa_rb_batch import run_batch
    from strategies.boll_reversal_strategy import BollReversalStrategy

    vt_symbol = f"{db_symbol}.{JM['exchange'].value}"
    df = run_batch(
        strategy_class=BollReversalStrategy,
        param_grid=BOLL_GRID,
        fixed_params={"fixed_size": 1},
        label=f"Boll/JM ({variant_label})",
        contracts=[(vt_symbol, JM["start"], END_DATE)],
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        bt_kwargs=JM["bt"],
        interval="1d",
        min_trades=MIN_TRADES,
        # skip_empty_folds defaults to True now (post-H7 fix); make it explicit.
        skip_empty_folds=True,
    )

    if df.empty:
        return {"variant": variant_label, "df": df, "folds": 0}

    oos = df["oos_sharpe"].dropna()
    return {
        "variant": variant_label,
        "df": df,
        "folds": len(df),
        "oos_sharpe_mean": float(oos.mean()),
        "oos_sharpe_median": float(oos.median()),
        "oos_sharpe_min": float(oos.min()),
        "oos_sharpe_max": float(oos.max()),
        "oos_positive_pct": float((oos > 0).mean() * 100),
        "total_oos_return_pct": float(df["oos_return_pct"].sum()),
        "is_oos_corr": float(df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]),
        "is_sharpe_mean": float(df["is_sharpe"].dropna().mean()),
    }


def classify(res: dict[str, Any]) -> str:
    if res["folds"] == 0:
        return "[FAIL_RUN]"
    s = res["oos_sharpe_mean"]
    pos = res["oos_positive_pct"]
    if s > 0.30 and pos > 60:
        return "[PROMOTE]"
    if s > 0.15 and pos > 55:
        return "[PARTIAL]"
    if s > 0:
        return "[WEAK]"
    return "[NO_EDGE]"


def _print_variant(res: dict[str, Any]) -> None:
    v = res["variant"]
    if res["folds"] == 0:
        print(f"  [{v}] FAIL — no folds produced")
        return
    print(
        f"  [{v}] {classify(res)}  folds={res['folds']}  "
        f"OOS Sharpe mean={res['oos_sharpe_mean']:+.3f}  "
        f"median={res['oos_sharpe_median']:+.3f}  "
        f"pos%={res['oos_positive_pct']:.1f}  "
        f"IS→OOS decay={res['oos_sharpe_mean'] - res['is_sharpe_mean']:+.3f}  "
        f"corr={res['is_oos_corr']:+.3f}  "
        f"total={res['total_oos_return_pct']:+.2f}%"
    )


def _print_param_distribution(df: pd.DataFrame, variant: str) -> None:
    if df.empty:
        return
    print(f"\n  [{variant}] IS-picked params (best by fold):")
    counts = df["best_params"].astype(str).value_counts()
    for params, n in counts.items():
        print(f"    {params}: {n} fold(s)")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)
    logging.getLogger("data_fetcher").setLevel(logging.WARNING)

    print(f"\n{'#' * 84}")
    print("# H7b: Boll daily on JM (焦煤) — adj15 (primary) vs raw")
    print(f"{'#' * 84}")
    print(f"  Grid: {BOLL_GRID}")
    print(f"  WFA: train={TRAIN_DAYS}d / test={TEST_DAYS}d / step={STEP_DAYS}d")
    print(f"  Min trades per fold: {MIN_TRADES} (empty folds skipped, not fatal)")
    print("  Reference (H7 DoubleMa raw): OOS -0.079, pos 31% → [NO_EDGE]")

    raw_symbol, adj_symbol = ensure_data()

    print(f"\n{'=' * 84}")
    print(f"=== Phase A: WFA on {adj_symbol} (H1.5 back-adjusted) — PRIMARY ===")
    print(f"{'=' * 84}")
    adj_res = run_wfa_variant(adj_symbol, "adj15")

    print(f"\n{'=' * 84}")
    print(f"=== Phase B: WFA on {raw_symbol} (raw continuous) — diagnostic only ===")
    print(f"{'=' * 84}")
    raw_res = run_wfa_variant(raw_symbol, "raw")

    print(f"\n\n{'=' * 84}")
    print("H7b RESULTS")
    print(f"{'=' * 84}")
    _print_variant(adj_res)
    _print_variant(raw_res)

    if adj_res["folds"] > 0:
        _print_param_distribution(adj_res["df"], "adj15")
    if raw_res["folds"] > 0:
        _print_param_distribution(raw_res["df"], "raw")

    out_dir = REPO_ROOT / "research"
    if adj_res["folds"] > 0:
        adj_res["df"].to_csv(out_dir / "wfa_results_h7b_jm_boll_adj15.csv", index=False)
    if raw_res["folds"] > 0:
        raw_res["df"].to_csv(out_dir / "wfa_results_h7b_jm_boll_raw.csv", index=False)

    print(f"\n{'=' * 84}")
    print("VERDICT")
    print(f"{'=' * 84}")

    # adj15 is the primary signal — raw is diagnostic only because σ math is
    # corrupted by rollover gaps. Verdict is driven by adj15.
    if adj_res["folds"] == 0:
        print("  adj15 produced 0 folds — cannot conclude. Investigate data range/min_trades.")
        return 1

    adj_tag = classify(adj_res)
    print(f"  Primary (adj15): {adj_tag}  OOS Sharpe={adj_res['oos_sharpe_mean']:+.3f}")
    if raw_res["folds"] > 0:
        delta = raw_res["oos_sharpe_mean"] - adj_res["oos_sharpe_mean"]
        print(
            f"  Cross-check (raw): OOS Sharpe={raw_res['oos_sharpe_mean']:+.3f}  "
            f"raw−adj15 delta={delta:+.3f}"
        )
        if abs(delta) > 0.30:
            print(
                "  → Large raw/adj15 divergence. For Boll this is expected: raw σ is "
                "polluted by rollover gaps. Trust adj15."
            )

    if adj_tag == "[PROMOTE]":
        print("\n  → JM/Boll is tradeable. Confirms H7's 31%-positive hint: JM IS")
        print("     mean-reverting at the daily horizon, DoubleMa was wrong-direction.")
        print("     Next step: lock most-frequent IS-winning params, add a")
        print("     BollReversalStrategy instance for jm_continuous_adj15.DCE to")
        print("     cta_strategy_setting.json, launch under QUANT_MODE=SIGNAL_ONLY.")
        print("     Observe ≥10 trading days before considering LIVE.")
    elif adj_tag == "[PARTIAL]":
        print("\n  → JM/Boll has weak edge. Consider:")
        print("     - widen boll_dev grid (try 3.5/4.0) if winners cluster at 3.0")
        print("     - add ATR stop (sl_atr_mult) to cap fat-tail losses")
        print("     - try Donchian — JM may want hard-channel logic, not σ-channel")
    elif adj_tag == "[WEAK]":
        print("\n  → JM/Boll barely positive. Not actionable. JM at daily horizon")
        print("     looks like noise + occasional policy shock. Either go intraday")
        print("     or skip JM.")
    else:
        print("\n  → JM/Boll does not produce edge either. Combined with H7's NO_EDGE,")
        print("     JM appears untradeable with standard daily TS-momentum or MR signals.")
        print("     Skip JM. Don't add to live stack.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
