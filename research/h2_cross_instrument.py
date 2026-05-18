"""H2: Cross-instrument validation of DoubleMa daily-continuous alpha.

DoubleMa/AG daily produced the only confirmed alpha so far: OOS Sharpe +0.424,
median +0.520, 76.5% positive folds across 17 folds AFTER H1.5 surgical
(OI-based) rollover removal. The question H2 answers is whether this signal
generalises to other commodity futures or AG is a singleton (regime/instrument-
specific artifact).

Universe: HC (hot-rolled coil, SHFE), I (iron ore, DCE), AU (gold, SHFE),
CU (copper, SHFE). Picked for diversity of sector (steel/precious/base-metal)
and rollover cadence (HC/I monthly-ish, AU/CU bi-monthly).

Pipeline per instrument:
  1. Fetch daily continuous from AkShare (XX0 symbol → {sym}_continuous in DB)
  2. Apply H1.5 OI-based detector (|ΔOI|>20% AND |gap|>0.3%) and diagnose
  3. Back-adjust and re-import as {sym}_continuous_adj15
  4. Run DoubleMa WFA with same grid + windows as AG baseline
  5. Compare against AG baseline (+0.424 mean, +0.520 median, 76.5% pos)

Verdict criteria:
  - 2+ instruments replicate (OOS Sharpe > +0.25 AND positive % > 60%):
      momentum is a real strategy family on Chinese commodities → ensemble
  - 1 weaker replicate: edge exists but instrument-sparse, AG is the strong
      anchor; consider widening universe further
  - 0 replicates: AG is a singleton, treat with caution; revisit selector
      problem (ensemble / alt-metric per priority list item 2/3)
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

from research.h1_5_calendar_rollover import (  # noqa: E402
    GAP_FLOOR_PCT,
    OI_PCT_THRESHOLD,
    back_adjust,
    detect_rollovers_calendar,
    detect_rollovers_oi,
    diagnose,
    load_bars_to_df,
)

# Per-instrument backtest spec. Sourced from exchange contract spec sheets.
#   - HC (SHFE hot-rolled coil): 10t/lot, tick 1元/t, fees ~0.01% incl. ex+broker.
#   - I  (DCE iron ore):         100t/lot, tick 0.5元/t, fees ~0.006%.
#   - AU (SHFE gold):            1000g/lot, tick 0.02元/g, fees ~0.005%.
#   - CU (SHFE copper):          5t/lot, tick 10元/t, fees ~0.005%.
# Slippage set to 1 tick per side to match AG baseline convention.
INSTRUMENTS: list[dict[str, Any]] = [
    {
        "sym": "hc",
        "ak_symbol": "HC0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 6,
        "start": datetime(2014, 3, 21),
        "bt": dict(capital=1_000_000, rate=1e-4, slippage=1, size=10, pricetick=1),
    },
    {
        "sym": "i",
        "ak_symbol": "I0",
        "exchange": Exchange.DCE,
        "expected_rolls_per_year": 6,
        "start": datetime(2013, 10, 18),
        "bt": dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=100, pricetick=0.5),
    },
    {
        "sym": "au",
        "ak_symbol": "AU0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 6,
        "start": datetime(2008, 1, 9),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=0.02, size=1000, pricetick=0.02),
    },
    {
        "sym": "cu",
        "ak_symbol": "CU0",
        "exchange": Exchange.SHFE,
        "expected_rolls_per_year": 12,  # CU has monthly deliveries
        "start": datetime(2005, 1, 4),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=10, size=5, pricetick=10),
    },
]

END_DATE = datetime(2026, 5, 15)

# Reuse identical WFA spec as DoubleMa/AG to make results directly comparable.
TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
MIN_TRADES = 5

# AG H1.5 baseline numbers (from research/h1_5_calendar_rollover.py output)
AG_BASELINE = {
    "label": "DoubleMa/AG daily (H1.5 adj)",
    "folds": 17,
    "oos_sharpe_mean": 0.424,
    "oos_sharpe_median": 0.520,
    "oos_positive_pct": 76.5,
    "total_oos_return_pct": 8.90,
    "is_oos_corr": -0.199,
}


def fetch_and_import_continuous(inst: dict[str, Any]) -> str:
    """Fetch AK XX0 daily, save CSV, import as {sym}_continuous in vn.py DB."""
    from import_data import import_csv_to_database
    from utils.data_fetcher import fetch_and_save

    csv_path = fetch_and_save(inst["ak_symbol"], timeframe="daily")
    db_symbol = f"{inst['sym']}_continuous"

    import_csv_to_database(
        csv_path=csv_path,
        symbol=db_symbol,
        exchange=inst["exchange"],
        interval=Interval.DAILY,
        batch_size=5000,
        resume=False,
    )
    return db_symbol


def adjust_and_import(inst: dict[str, Any], src_symbol: str) -> tuple[str, dict[str, Any]]:
    """Detect rollovers, back-adjust, import as {sym}_continuous_adj15. Returns
    (db_symbol, diagnostics)."""
    df_orig = load_bars_to_df(src_symbol, inst["exchange"], Interval.DAILY)
    n_bars = len(df_orig)
    years = (
        pd.to_datetime(df_orig["datetime"].iloc[-1]) - pd.to_datetime(df_orig["datetime"].iloc[0])
    ).days / 365.25
    expected_rolls = int(years * inst["expected_rolls_per_year"])
    print(f"\n  [{inst['sym'].upper()}] {n_bars} bars over {years:.1f} years")
    print(f"  Expected rollovers ≈ {expected_rolls} (at {inst['expected_rolls_per_year']}/yr)")

    has_oi = (df_orig["open_interest"].fillna(0) > 0).any()
    print(f"  open_interest present: {has_oi}")

    # H1 crude detector for comparison
    prev_close = df_orig["close"].shift(1)
    gap_abs_pct = ((df_orig["open"] - prev_close) / prev_close * 100).abs()
    crude_mask = (gap_abs_pct > 1.5).fillna(False)
    diagnose(df_orig, crude_mask, f"H1 baseline (|gap|>1.5%) on {inst['sym'].upper()}")

    if has_oi:
        precise_mask = detect_rollovers_oi(df_orig, OI_PCT_THRESHOLD, GAP_FLOOR_PCT)
        label = f"H1.5 OI-based (|ΔOI|>{OI_PCT_THRESHOLD}% AND |gap|>{GAP_FLOOR_PCT}%)"
    else:
        precise_mask = detect_rollovers_calendar(df_orig)
        label = "H1.5 calendar-fallback (no OI data)"
    diagnose(df_orig, precise_mask, f"{label} on {inst['sym'].upper()}")

    n_flagged = int(precise_mask.sum())
    fit_ratio = n_flagged / max(expected_rolls, 1)
    print(
        f"  Detector fit: {n_flagged}/{expected_rolls} = {fit_ratio:.2f}x expected "
        f"({'OK' if 0.5 < fit_ratio < 2.0 else 'OUT OF BAND — interpret cautiously'})"
    )

    df_adj = back_adjust(df_orig, precise_mask)
    new_symbol = f"{inst['sym']}_continuous_adj15"

    csv_dir = REPO_ROOT / "data" / "bar"
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / f"{new_symbol}_daily.csv"
    df_adj.to_csv(csv_path, index=False)

    from import_data import import_csv_to_database

    import_csv_to_database(
        csv_path=csv_path,
        symbol=new_symbol,
        exchange=inst["exchange"],
        interval=Interval.DAILY,
        batch_size=5000,
        resume=False,
    )

    diagnostics = {
        "n_bars": n_bars,
        "years": years,
        "expected_rolls": expected_rolls,
        "n_flagged": n_flagged,
        "fit_ratio": fit_ratio,
        "has_oi": has_oi,
    }
    return new_symbol, diagnostics


def run_wfa_on(inst: dict[str, Any], adj_symbol: str) -> dict[str, Any]:
    """Run DoubleMa WFA on the adj symbol with same grid as AG baseline."""
    from research.wfa_rb_batch import run_batch
    from strategies.double_ma_strategy import DoubleMaStrategy

    vt_symbol = f"{adj_symbol}.{inst['exchange'].value}"
    contracts = [(vt_symbol, inst["start"], END_DATE)]
    df = run_batch(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": 1},
        label=f"DoubleMa/{inst['sym'].upper()}",
        contracts=contracts,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        bt_kwargs=inst["bt"],
        interval="1d",
        min_trades=MIN_TRADES,
    )

    if df.empty:
        return {
            "df": df,
            "folds": 0,
            "oos_sharpe_mean": float("nan"),
            "oos_sharpe_median": float("nan"),
            "oos_positive_pct": float("nan"),
            "total_oos_return_pct": float("nan"),
            "is_oos_corr": float("nan"),
        }

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


def classify(result: dict[str, Any]) -> str:
    """Single-instrument verdict tag for the summary table."""
    if result["folds"] == 0:
        return "[FAIL_RUN]"
    s = result["oos_sharpe_mean"]
    pos = result["oos_positive_pct"]
    if s > 0.25 and pos > 60:
        return "[REPLICATES]"
    if s > 0.15 and pos > 55:
        return "[PARTIAL]"
    if s > 0:
        return "[WEAK]"
    return "[NO_EDGE]"


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)
    logging.getLogger("data_fetcher").setLevel(logging.WARNING)

    print(
        f"\n{'#' * 84}\n# H2: Cross-instrument DoubleMa daily validation (vs. AG baseline)\n{'#' * 84}"
    )
    print(f"  Universe: {[i['sym'].upper() for i in INSTRUMENTS]}")
    print(f"  Grid: {DM_GRID}  |  WFA: train={TRAIN_DAYS} test={TEST_DAYS} step={STEP_DAYS}")
    print(
        f"  AG baseline to beat: Sharpe +{AG_BASELINE['oos_sharpe_mean']}, "
        f"median +{AG_BASELINE['oos_sharpe_median']}, pos% {AG_BASELINE['oos_positive_pct']}"
    )

    all_results: list[dict[str, Any]] = []
    all_dfs: list[pd.DataFrame] = []

    for inst in INSTRUMENTS:
        print(
            f"\n{'=' * 84}\n=== {inst['sym'].upper()} ({inst['ak_symbol']}, {inst['exchange'].value}) ===\n{'=' * 84}"
        )

        print("\n--- Phase 1: fetch + import continuous ---")
        try:
            src_symbol = fetch_and_import_continuous(inst)
        except Exception as e:
            print(f"  FETCH FAILED: {type(e).__name__}: {e}")
            all_results.append({"sym": inst["sym"], "stage": "fetch", "error": str(e)})
            continue

        print("\n--- Phase 2: detect + back-adjust rollovers ---")
        try:
            adj_symbol, diag = adjust_and_import(inst, src_symbol)
        except Exception as e:
            print(f"  ADJUST FAILED: {type(e).__name__}: {e}")
            all_results.append({"sym": inst["sym"], "stage": "adjust", "error": str(e)})
            continue

        print(f"\n--- Phase 3: WFA DoubleMa on {adj_symbol} ---")
        try:
            res = run_wfa_on(inst, adj_symbol)
        except Exception as e:
            print(f"  WFA FAILED: {type(e).__name__}: {e}")
            all_results.append({"sym": inst["sym"], "stage": "wfa", "error": str(e)})
            continue

        if not res["df"].empty:
            res["df"].insert(0, "sym", inst["sym"])
            all_dfs.append(res["df"])

        verdict = classify(res)
        print(
            f"\n  [{inst['sym'].upper()}] {verdict}  folds={res['folds']}  "
            f"OOS Sharpe mean={res['oos_sharpe_mean']:+.3f}  median={res['oos_sharpe_median']:+.3f}  "
            f"pos%={res['oos_positive_pct']:.1f}  IS-OOS corr={res['is_oos_corr']:+.3f}  "
            f"total return={res['total_oos_return_pct']:+.2f}%"
        )

        all_results.append(
            {
                "sym": inst["sym"],
                "stage": "ok",
                "verdict": verdict,
                **diag,
                **{k: v for k, v in res.items() if k != "df"},
            }
        )

    # Comparison table
    print(f"\n\n{'=' * 100}\nH2 COMPARISON TABLE (DoubleMa daily on adj15 continuous)\n{'=' * 100}")
    header = f"  {'Instrument':12s} {'Verdict':14s} {'Folds':>6s} {'OOS Sharpe':>11s} {'Median':>9s} {'Pos %':>7s} {'IS-OOS corr':>13s} {'Total %':>9s}"
    print(header)
    print("  " + "-" * 90)
    # AG baseline row
    print(
        f"  {'AG (baseline)':12s} {'[ANCHOR]':14s} {AG_BASELINE['folds']:>6d} "
        f"{AG_BASELINE['oos_sharpe_mean']:>+11.3f} {AG_BASELINE['oos_sharpe_median']:>+9.3f} "
        f"{AG_BASELINE['oos_positive_pct']:>7.1f} {AG_BASELINE['is_oos_corr']:>+13.3f} "
        f"{AG_BASELINE['total_oos_return_pct']:>+9.2f}"
    )
    print("  " + "-" * 90)
    for r in all_results:
        if r.get("stage") != "ok":
            print(
                f"  {r['sym'].upper():12s} {'[' + r.get('stage', '?').upper() + '_FAIL]':14s}  {r.get('error', '')}"
            )
            continue
        print(
            f"  {r['sym'].upper():12s} {r['verdict']:14s} {r['folds']:>6d} "
            f"{r['oos_sharpe_mean']:>+11.3f} {r['oos_sharpe_median']:>+9.3f} "
            f"{r['oos_positive_pct']:>7.1f} {r['is_oos_corr']:>+13.3f} "
            f"{r['total_oos_return_pct']:>+9.2f}"
        )

    # Verdict
    replicates = [r for r in all_results if r.get("verdict") == "[REPLICATES]"]
    partials = [r for r in all_results if r.get("verdict") == "[PARTIAL]"]
    print(f"\n{'=' * 100}\nH2 VERDICT\n{'=' * 100}")
    print(
        f"  Full replicates (Sharpe>+0.25, pos%>60): {len(replicates)} — {[r['sym'] for r in replicates]}"
    )
    print(
        f"  Partial replicates (Sharpe>+0.15, pos%>55): {len(partials)} — {[r['sym'] for r in partials]}"
    )

    if len(replicates) >= 2:
        print("  [FAMILY] Momentum (DoubleMa daily) is a CROSS-INSTRUMENT alpha family.")
        print("           AG is not a singleton. Build a multi-instrument portfolio with")
        print("           per-instrument WFA-picked params + equal-risk weighting.")
        print("           Next: H4 ensemble across confirmed family + paper-trade prep.")
    elif len(replicates) == 1 or partials:
        names = [r["sym"] for r in replicates + partials]
        print(f"  [SPARSE] Momentum found on AG + {names} only. Edge is instrument-")
        print("           sensitive — caution required. Possible drivers: storage cost,")
        print("           open-interest structure, contract spec (margin/leverage).")
        print("           Next: widen universe (NI, SC, RU, IF) OR pivot to selection fix")
        print("           (priority 2 ensemble / priority 3 alt-metric).")
    else:
        print("  [SINGLETON] AG is alone. No cross-instrument momentum replication.")
        print("              DoubleMa/AG +0.42 Sharpe likely an instrument-specific")
        print("              regime artifact (silver inflation hedge / 2020-2024 squeeze).")
        print("              Demote AG to candidate-only. Pivot to priority 2 (ensemble)")
        print("              or priority 3 (alt-metric) for the selector problem.")

    # Save artefacts
    out_dir = REPO_ROOT / "research"
    if all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True)
        combined.to_csv(out_dir / "wfa_results_h2_cross_instrument.csv", index=False)
        print(f"\nFull fold table → {out_dir / 'wfa_results_h2_cross_instrument.csv'}")

    summary_df = pd.DataFrame(all_results)
    summary_df.to_csv(out_dir / "wfa_summary_h2.csv", index=False)
    print(f"Summary table   → {out_dir / 'wfa_summary_h2.csv'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
