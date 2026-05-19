"""H5: Ratio-based back-adjustment for daily continuous futures.

Background (see [[project-backadjust-universality]]). H1.5 additive
back-adjustment is calibrated for AG and degrades across the family:

  AG  → mild improvement (baseline)
  HC  → neutral
  CU  → strong improvement (+0.43 Sharpe over raw)
  AU  → hurts (over-scrubs macro-driven OI days)
  I   → BROKEN (additive accumulation under multi-year contango drives
                adj close to -775 RMB; vn.py rejects negative limit prices)

Root issue: additive shifts every prior bar by the cumulative SUM of
rollover gaps. Under persistent contango/backwardation that sum can exceed
the original price level. Ratio-based adjustment (standard at CSI, CQG,
Bloomberg) is multiplicative — it preserves positivity AND percentage
returns by construction, removing the per-instrument calibration burden.

This script:
  1. Defines ratio_back_adjust(df, mask) using the same H1.5 OI mask
  2. Generates {sym}_continuous_adj15r symbols for AG/HC/I/AU/CU
  3. Runs DoubleMa daily WFA with identical config to H2
  4. Compares ratio-adj Sharpe vs raw and additive-adj baselines

Goals (priority order):
  1. UNBLOCK I (currently the H4 ensemble's intersection-start gate)
  2. Confirm CU's +0.568 Sharpe holds under the new adjuster
  3. Confirm AG/HC/AU don't regress
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
    diagnose,
    load_bars_to_df,
)

# Per-instrument backtest spec (matches the configs that produced the
# baseline Sharpe numbers we will compare against in the verdict table).
#   - AG uses AG_BT (size=15) so AG.adj15r compares to AG.adj15 +0.424
#   - HC/I/AU/CU use h2_cross_instrument.INSTRUMENTS bt dicts so the
#     ratio numbers compare directly to the raw/adj15 numbers from
#     h2_followup_raw_vs_adj.py.
INSTRUMENTS: list[dict[str, Any]] = [
    {
        "sym": "ag",
        "exchange": Exchange.SHFE,
        "start": datetime(2012, 5, 10),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=1, size=15, pricetick=1),
    },
    {
        "sym": "hc",
        "exchange": Exchange.SHFE,
        "start": datetime(2014, 3, 21),
        "bt": dict(capital=1_000_000, rate=1e-4, slippage=1, size=10, pricetick=1),
    },
    {
        "sym": "i",
        "exchange": Exchange.DCE,
        "start": datetime(2013, 10, 18),
        "bt": dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=100, pricetick=0.5),
    },
    {
        "sym": "au",
        "exchange": Exchange.SHFE,
        "start": datetime(2008, 1, 9),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=0.02, size=1000, pricetick=0.02),
    },
    {
        "sym": "cu",
        "exchange": Exchange.SHFE,
        "start": datetime(2005, 1, 4),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=10, size=5, pricetick=10),
    },
]

END_DATE = datetime(2026, 5, 15)
TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
MIN_TRADES = 5

# Baseline numbers from prior runs (h1_5_calendar_rollover.py for AG,
# h2_cross_instrument.py + h2_followup_raw_vs_adj.py for the rest). These
# are the targets H5 must match or beat.
BASELINES: dict[str, dict[str, dict[str, float] | None]] = {
    "ag": {
        "raw": {"sharpe": 0.344, "pos_pct": 64.7},
        "adj15": {"sharpe": 0.424, "pos_pct": 76.5},
    },
    "hc": {
        "raw": {"sharpe": 0.070, "pos_pct": 42.9},
        "adj15": {"sharpe": 0.091, "pos_pct": 50.0},
    },
    "i": {
        "raw": {"sharpe": 0.445, "pos_pct": 73.3},
        "adj15": None,  # broken — additive drove prices negative
    },
    "au": {
        "raw": {"sharpe": 0.161, "pos_pct": 54.2},
        "adj15": {"sharpe": -0.135, "pos_pct": 45.8},
    },
    "cu": {
        "raw": {"sharpe": 0.138, "pos_pct": 60.7},
        "adj15": {"sharpe": 0.568, "pos_pct": 60.7},
    },
}


def ratio_back_adjust(df: pd.DataFrame, rollover_mask: pd.Series) -> pd.DataFrame:
    """Scale prior bars by the cumulative rollover-day price ratio.

    On a rollover day, ratio = open / prev_close. The cumulative product
    of ratios for all rollover days strictly AFTER row i is the
    adjustment multiplier for row i. By construction this preserves
    positivity (product of positives stays positive) and percentage
    returns between rollover days (a constant rescaling is Sharpe-
    invariant); only the rollover day's own bar is "absorbed" into the
    new contract regime — the prior bar's close is multiplied by the
    ratio so it equals today's open, smoothly bridging the splice.
    """
    df = df.copy().reset_index(drop=True)
    prev_close = df["close"].shift(1)
    ratio = (df["open"] / prev_close).where(rollover_mask, 1.0)
    # Defensive: first row's prev_close is NaN; zero/inf cases get 1.0
    ratio = ratio.fillna(1.0).replace([np.inf, -np.inf], 1.0)

    # Reverse cumulative product mirrors the additive reverse-cumsum.
    rev_cumprod = ratio[::-1].cumprod()[::-1]
    # Divide out the current row's ratio so the rollover day itself stays
    # in the new-contract regime (same as cum_adj - gap_at_rollovers does
    # for the additive version).
    cum_ratio = rev_cumprod / ratio

    for col in ["open", "high", "low", "close"]:
        df[col] = df[col] * cum_ratio
    return df


def adjust_and_import(inst: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Load raw, ratio-adjust, export CSV, import as {sym}_continuous_adj15r."""
    from import_data import import_csv_to_database

    sym = inst["sym"]
    src_symbol = f"{sym}_continuous"
    df_orig = load_bars_to_df(src_symbol, inst["exchange"], Interval.DAILY)
    n_bars = len(df_orig)
    if n_bars == 0:
        raise RuntimeError(
            f"No bars found for {src_symbol}.{inst['exchange'].value} — "
            f"run h2_cross_instrument.py or similar to populate the raw symbol."
        )

    years = (
        pd.to_datetime(df_orig["datetime"].iloc[-1]) - pd.to_datetime(df_orig["datetime"].iloc[0])
    ).days / 365.25
    print(f"\n  [{sym.upper()}] {n_bars} bars over {years:.1f} years")

    has_oi = (df_orig["open_interest"].fillna(0) > 0).any()
    if not has_oi:
        raise RuntimeError(
            f"{src_symbol} has no open_interest — H5 ratio adjustment requires "
            f"H1.5 OI-based detection. Calendar fallback is for additive only."
        )

    mask = detect_rollovers_oi(df_orig, OI_PCT_THRESHOLD, GAP_FLOOR_PCT)
    diagnose(
        df_orig,
        mask,
        f"H1.5 OI mask (|ΔOI|>{OI_PCT_THRESHOLD}% AND |gap|>{GAP_FLOOR_PCT}%) on {sym.upper()}",
    )

    df_adj = ratio_back_adjust(df_orig, mask)

    # Invariants
    ohlc = df_adj[["open", "high", "low", "close"]]
    pre_min = float(df_orig[["open", "high", "low", "close"]].min().min())
    post_min = float(ohlc.min().min())
    pre_max = float(df_orig[["open", "high", "low", "close"]].max().max())
    post_max = float(ohlc.max().max())
    print(
        f"  OHLC range pre-adj : [{pre_min:,.4f}, {pre_max:,.4f}]\n"
        f"  OHLC range post-adj: [{post_min:,.4f}, {post_max:,.4f}]"
    )
    if post_min <= 0:
        raise RuntimeError(
            f"{sym.upper()} ratio-adjusted prices non-positive (min={post_min}). "
            f"Should be mathematically impossible — investigate ratio_back_adjust."
        )
    # Last bar unchanged (cum_ratio==1)
    last_close_drift = abs(float(df_adj["close"].iloc[-1]) - float(df_orig["close"].iloc[-1]))
    print(f"  Last-bar close drift (should be ~0): {last_close_drift:.6f}")

    new_symbol = f"{sym}_continuous_adj15r"
    csv_dir = REPO_ROOT / "data" / "bar"
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"{new_symbol}_daily.csv"
    df_adj.to_csv(csv_path, index=False)

    import_csv_to_database(
        csv_path=csv_path,
        symbol=new_symbol,
        exchange=inst["exchange"],
        interval=Interval.DAILY,
        batch_size=5000,
        resume=False,
    )

    diag = {
        "n_bars": n_bars,
        "years": years,
        "n_flagged": int(mask.sum()),
        "pre_min": pre_min,
        "post_min": post_min,
        "pre_max": pre_max,
        "post_max": post_max,
    }
    return new_symbol, diag


def run_wfa_on(inst: dict[str, Any], adj_symbol: str) -> dict[str, Any]:
    from research.wfa import run_wfa
    from strategies.double_ma_strategy import DoubleMaStrategy

    vt_symbol = f"{adj_symbol}.{inst['exchange'].value}"
    df = run_wfa(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": 1},
        vt_symbol=vt_symbol,
        interval="1d",
        start=inst["start"],
        end=END_DATE,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        **inst["bt"],
    )
    if len(df) == 0:
        return {"folds": 0}
    oos = df["oos_sharpe"].dropna()
    return {
        "df": df,
        "folds": len(df),
        "oos_sharpe_mean": float(oos.mean()),
        "oos_sharpe_median": float(oos.median()),
        "oos_positive_pct": float((oos > 0).mean() * 100),
        "is_oos_corr": float(df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]),
        "total_oos_return_pct": float(df["oos_return_pct"].sum()),
    }


def classify_vs_baselines(sym: str, ratio_res: dict) -> tuple[str, str]:
    """Compare adj15r Sharpe to raw and additive-adj15 baselines."""
    if ratio_res.get("folds", 0) == 0:
        return "[RATIO_BROKEN]", "n/a (0 folds)"

    base = BASELINES[sym]
    raw_s = base["raw"]["sharpe"] if base["raw"] else None
    adj_s = base["adj15"]["sharpe"] if base["adj15"] else None
    ratio_s = ratio_res["oos_sharpe_mean"]

    candidates = [v for v in [raw_s, adj_s] if v is not None]
    best_existing = max(candidates) if candidates else None
    if best_existing is None:
        return "[RATIO_NEW_BASELINE]", f"{ratio_s:+.3f} (no prior to compare)"

    delta = ratio_s - best_existing
    if delta > 0.05:
        tag = "[RATIO_WINS]"
    elif delta > -0.05:
        tag = "[RATIO_TIES]"
    else:
        tag = "[RATIO_LOSES]"
    return tag, f"{ratio_s:+.3f} vs best({best_existing:+.3f}) Δ={delta:+.3f}"


def family_verdict(rows: list[dict]) -> str:
    """Apply H5 family-level decision rule from the plan."""
    by_sym = {r["sym"]: r for r in rows if r.get("ratio") and r["ratio"].get("folds", 0) > 0}
    i_s = by_sym.get("i", {}).get("ratio", {}).get("oos_sharpe_mean", float("nan"))
    cu_s = by_sym.get("cu", {}).get("ratio", {}).get("oos_sharpe_mean", float("nan"))
    ag_s = by_sym.get("ag", {}).get("ratio", {}).get("oos_sharpe_mean", float("nan"))

    i_unblocked = i_s > 0.40
    cu_holds = cu_s > 0.45
    ag_holds = ag_s > 0.30

    if i_unblocked and cu_holds and ag_holds:
        return (
            "[GREEN] I unblocked, CU & AG hold. Promote adj15r as the\n"
            "        canonical adjuster. Next: H6 — rebuild H4 ensemble with\n"
            "        ag_continuous_adj15r + i_continuous_adj15r + cu_continuous_adj15r,\n"
            "        starting from I's full 2013-10 history."
        )
    if i_unblocked and not cu_holds:
        return (
            "[AMBER] I unblocked but CU regresses. Keep additive (CU.adj15)\n"
            "        for the ensemble's heaviest carrier; use ratio (I.adj15r)\n"
            "        only to extend I's history. Mixed-adjuster ensemble in H6."
        )
    if not i_unblocked:
        return (
            "[RED] I.adj15r failed to clear +0.40 Sharpe — ratio adjustment did not\n"
            "      restore the raw I signal. Keep H4 ensemble at AG.adj15+I.raw+CU.adj15.\n"
            "      Investigate whether I's edge is inherent to raw splicing (carry\n"
            "      premium IS the signal) before further methodology work."
        )
    return "[UNCLASSIFIED] — check per-instrument table above."


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 90}\n# H5: ratio-based back-adjustment vs raw / additive adj15\n{'#' * 90}")
    print(f"  Universe: {[i['sym'].upper() for i in INSTRUMENTS]}")
    print("  Adjuster: ratio_back_adjust using H1.5 OI mask")
    print(f"  Grid: {DM_GRID}  |  WFA: train={TRAIN_DAYS} test={TEST_DAYS} step={STEP_DAYS}")

    rows: list[dict[str, Any]] = []
    all_dfs: list[pd.DataFrame] = []

    for inst in INSTRUMENTS:
        sym = inst["sym"]
        print(f"\n{'=' * 90}\n=== {sym.upper()} ({inst['exchange'].value}) ===\n{'=' * 90}")

        print("\n--- Phase 1: ratio-adjust + import ---")
        try:
            adj_symbol, diag = adjust_and_import(inst)
        except Exception as e:
            print(f"  ADJUST FAILED: {type(e).__name__}: {e}")
            rows.append({"sym": sym, "stage": "adjust", "error": str(e), "ratio": {"folds": 0}})
            continue

        print(f"\n--- Phase 2: WFA on {adj_symbol} ---")
        try:
            res = run_wfa_on(inst, adj_symbol)
        except Exception as e:
            print(f"  WFA FAILED: {type(e).__name__}: {e}")
            rows.append({"sym": sym, "stage": "wfa", "error": str(e), "ratio": {"folds": 0}})
            continue

        if res.get("folds", 0) > 0 and "df" in res:
            res["df"].insert(0, "source", "adj15r")
            res["df"].insert(0, "sym", sym)
            all_dfs.append(res["df"])

        tag, detail = classify_vs_baselines(sym, res)
        if res.get("folds", 0) > 0:
            print(
                f"\n  [{sym.upper()}] {tag}  folds={res['folds']}  "
                f"OOS Sharpe={res['oos_sharpe_mean']:+.3f}  median={res['oos_sharpe_median']:+.3f}  "
                f"pos%={res['oos_positive_pct']:.1f}  IS-OOS corr={res['is_oos_corr']:+.3f}  "
                f"total={res['total_oos_return_pct']:+.2f}%"
            )
        else:
            print(f"\n  [{sym.upper()}] {tag}  ({detail})")

        rows.append({"sym": sym, "stage": "ok", "ratio": res, "diag": diag, "tag": tag})

    # Comparison table: raw vs additive-adj15 vs ratio-adj15r
    print(f"\n\n{'=' * 116}\nH5 CONSOLIDATED TABLE\n{'=' * 116}")
    header = (
        f"  {'Sym':4s} | {'Raw S/pos%':>16s} | {'Adj15 S/pos%':>16s} | "
        f"{'Adj15r S/pos%':>17s} | {'Tag':>20s}"
    )
    print(header)
    print("  " + "-" * 100)
    for r in rows:
        sym = r["sym"]
        base = BASELINES[sym]
        raw_str = (
            f"{base['raw']['sharpe']:+.3f} / {base['raw']['pos_pct']:.1f}" if base["raw"] else "n/a"
        )
        adj_str = (
            f"{base['adj15']['sharpe']:+.3f} / {base['adj15']['pos_pct']:.1f}"
            if base["adj15"]
            else "broken"
        )
        ratio = r.get("ratio", {})
        if ratio.get("folds", 0) > 0:
            ratio_str = f"{ratio['oos_sharpe_mean']:+.3f} / {ratio['oos_positive_pct']:.1f}"
        else:
            ratio_str = r.get("error", "0 folds")[:18]
        tag = r.get("tag", "[FAIL]")
        print(
            f"  {sym.upper():4s} | {raw_str:>16s} | {adj_str:>16s} | {ratio_str:>17s} | {tag:>20s}"
        )

    # Family verdict
    print(f"\n{'=' * 116}\nH5 FAMILY VERDICT\n{'=' * 116}")
    print(family_verdict(rows))

    # Save artefacts
    out_dir = REPO_ROOT / "research"
    if all_dfs:
        out_path = out_dir / "wfa_results_h5_ratio.csv"
        pd.concat(all_dfs, ignore_index=True).to_csv(out_path, index=False)
        print(f"\nFull fold table → {out_path}")

    summary_rows = []
    for r in rows:
        ratio = r.get("ratio", {})
        base = BASELINES[r["sym"]]
        summary_rows.append(
            {
                "sym": r["sym"].upper(),
                "raw_sharpe": base["raw"]["sharpe"] if base["raw"] else None,
                "raw_pos_pct": base["raw"]["pos_pct"] if base["raw"] else None,
                "adj15_sharpe": base["adj15"]["sharpe"] if base["adj15"] else None,
                "adj15_pos_pct": base["adj15"]["pos_pct"] if base["adj15"] else None,
                "adj15r_folds": ratio.get("folds"),
                "adj15r_sharpe": ratio.get("oos_sharpe_mean"),
                "adj15r_median": ratio.get("oos_sharpe_median"),
                "adj15r_pos_pct": ratio.get("oos_positive_pct"),
                "adj15r_is_oos_corr": ratio.get("is_oos_corr"),
                "adj15r_total_return_pct": ratio.get("total_oos_return_pct"),
                "tag": r.get("tag", "[FAIL]"),
                "stage": r.get("stage"),
                "error": r.get("error"),
            }
        )
    summary_path = out_dir / "wfa_summary_h5.csv"
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False)
    print(f"Summary table   → {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
