"""M3: Apply Purged Walk-Forward (CPCV-PWF) to H4 ensemble's three instruments.

Background. H4 ensemble (AG.adj15 + I.raw + CU.adj15 equal-risk-weighted)
reports OOS Sharpe +0.993 from wfa.run_wfa with train=700d/test=250d/step=250d.
The H4 study also flagged IS-OOS Sharpe correlation ≈ -0.20 per instrument —
meaning the IS grid selector picks bad params almost as often as good ones.

The PWF smoke (research/cpcv.py main) reproduced this on AG with even worse
correlation (-0.607) but a HIGHER OOS Sharpe (+0.750 vs H4's +0.424 single-
instrument). Two questions for M3:

  (1) Does the +0.4 to +0.6 single-instrument OOS Sharpe hold under PWF
      across all three H4 members, or is AG a lucky outlier?
  (2) Does each instrument's PWF OOS Sharpe distribution support the H4
      ensemble's +0.993 claim, or does the cross-validation reveal that
      H4's number sits at the high end of a wide distribution?

This script does NOT recompute the ensemble Sharpe (would need to capture
per-split daily PnL curves — left for M3.5). It runs per-instrument PWF +
classic wfa.run_wfa side-by-side and reports both distributions, so the
discrepancy is observable.

Output: research/m3_h4_cpcv_summary.csv with per-instrument stats from both
methods, plus a human-readable VERDICT block.
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

from research.cpcv import run_pwf, summarize  # noqa: E402
from research.wfa import run_wfa  # noqa: E402

# Same instrument spec as h4_ensemble.py — keep apples-to-apples.
INSTRUMENTS: list[dict[str, Any]] = [
    {
        "sym": "AG",
        "vt_symbol": "ag_continuous_adj15.SHFE",
        "start": datetime(2012, 5, 10),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=1, size=15, pricetick=1),
    },
    {
        "sym": "I",
        "vt_symbol": "i_continuous.DCE",
        "start": datetime(2013, 10, 18),
        "bt": dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=100, pricetick=0.5),
    },
    {
        "sym": "CU",
        "vt_symbol": "cu_continuous_adj15.SHFE",
        "start": datetime(2005, 1, 4),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=10, size=5, pricetick=10),
    },
]

END_DATE = datetime(2026, 5, 15)
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
MIN_TRADES = 5

# H4 baseline settings (kept exact for direct comparison)
WFA_TRAIN_DAYS = 700
WFA_TEST_DAYS = 250
WFA_STEP_DAYS = 250

# PWF: pick n_folds so test window ≈ 250d (matches WFA test_days) for visual
# comparability. For AG (~14yr ≈ 5100 cal days), n_folds=10 → ~510 cal days
# per fold. CU is 21 years → fold = ~770 days. Tradeoff between density and
# test-window stability.
PWF_N_FOLDS = 10
PWF_PURGE_DAYS = 20


def run_one_instrument(inst: dict[str, Any]) -> dict[str, Any]:
    from strategies.double_ma_strategy import DoubleMaStrategy

    sym = inst["sym"]
    print(f"\n{'=' * 100}\n=== {sym} ({inst['vt_symbol']}) ===\n{'=' * 100}")

    # --- WFA baseline (H4 settings) ---
    print(f"\n  [WFA baseline] train={WFA_TRAIN_DAYS}d test={WFA_TEST_DAYS}d step={WFA_STEP_DAYS}d")
    df_wfa = run_wfa(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": 1},
        vt_symbol=inst["vt_symbol"],
        interval="1d",
        start=inst["start"],
        end=END_DATE,
        train_days=WFA_TRAIN_DAYS,
        test_days=WFA_TEST_DAYS,
        step_days=WFA_STEP_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        **inst["bt"],
    )
    df_wfa.insert(0, "method", "WFA")

    # --- PWF (M3 method) ---
    print(f"\n  [PWF] n_folds={PWF_N_FOLDS} purge={PWF_PURGE_DAYS}d")
    df_pwf = run_pwf(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": 1},
        vt_symbol=inst["vt_symbol"],
        interval="1d",
        start=inst["start"],
        end=END_DATE,
        n_folds=PWF_N_FOLDS,
        purge_days=PWF_PURGE_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        **inst["bt"],
    )
    df_pwf.insert(0, "method", "PWF")

    # Summaries — `summarize` expects an `is_sharpe` column; both methods have it
    wfa_stats = summarize(df_wfa, label=f"{sym} WFA")
    pwf_stats = summarize(df_pwf, label=f"{sym} PWF")

    return {
        "sym": sym,
        "df_wfa": df_wfa,
        "df_pwf": df_pwf,
        "wfa_stats": wfa_stats,
        "pwf_stats": pwf_stats,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)
    logging.getLogger("cpcv").setLevel(logging.WARNING)

    print(f"\n{'#' * 100}\n# M3: H4 ensemble per-instrument CPCV-PWF vs WFA baseline\n{'#' * 100}")
    print(f"  Strategy: DoubleMa daily  |  Grid: {DM_GRID}  |  min_trades={MIN_TRADES}")
    print(
        f"  WFA: train={WFA_TRAIN_DAYS}d test={WFA_TEST_DAYS}d step={WFA_STEP_DAYS}d (H4 baseline)"
    )
    print(f"  PWF: n_folds={PWF_N_FOLDS} purge={PWF_PURGE_DAYS}d (M3 method)")

    all_results: list[dict[str, Any]] = []
    all_dfs: list[pd.DataFrame] = []
    for inst in INSTRUMENTS:
        r = run_one_instrument(inst)
        all_results.append(r)
        for which in ("df_wfa", "df_pwf"):
            d = r[which].copy()
            d.insert(0, "sym", r["sym"])
            all_dfs.append(d)

    # --- Cross-instrument summary table ---
    print(
        f"\n\n{'=' * 110}\nCROSS-INSTRUMENT SUMMARY (OOS Sharpe distribution per method)\n{'=' * 110}"
    )
    header = (
        f"  {'inst':4s}  {'method':4s}  {'n':>3s}  {'mean':>7s}  {'median':>7s}  "
        f"{'std':>6s}  {'q25':>7s}  {'q75':>7s}  {'pos%':>5s}  {'IS-OOS corr':>11s}"
    )
    print(header)
    print("  " + "-" * 95)
    for r in all_results:
        for stats_key in ("wfa_stats", "pwf_stats"):
            s = r[stats_key]
            method = "WFA" if stats_key == "wfa_stats" else "PWF"
            if s.get("n_splits", 0) == 0:
                print(f"  {r['sym']:4s}  {method:4s}  no data")
                continue
            print(
                f"  {r['sym']:4s}  {method:4s}  {s['n_splits']:>3d}  "
                f"{s['oos_sharpe_mean']:>+7.3f}  {s['oos_sharpe_median']:>+7.3f}  "
                f"{s['oos_sharpe_std']:>6.3f}  {s['oos_sharpe_q25']:>+7.3f}  "
                f"{s['oos_sharpe_q75']:>+7.3f}  {s['oos_positive_pct']:>4.1f}%  "
                f"{s['is_oos_corr']:>+11.3f}"
            )

    # --- Save artefacts ---
    combined_df = pd.concat(all_dfs, ignore_index=True)
    out = REPO_ROOT / "research" / "m3_h4_cpcv_summary.csv"
    combined_df.to_csv(out, index=False)
    print(f"\nFull fold table → {out}")

    summary_rows = []
    for r in all_results:
        for stats_key in ("wfa_stats", "pwf_stats"):
            s = r[stats_key]
            method = "WFA" if stats_key == "wfa_stats" else "PWF"
            summary_rows.append({"sym": r["sym"], "method": method, **s})
    summary_df = pd.DataFrame(summary_rows)
    out_sum = REPO_ROOT / "research" / "m3_h4_cpcv_stats.csv"
    summary_df.to_csv(out_sum, index=False)
    print(f"Per-method stats → {out_sum}")

    # --- Verdict ---
    print(f"\n{'=' * 110}\nVERDICT\n{'=' * 110}")
    for r in all_results:
        w = r["wfa_stats"]
        p = r["pwf_stats"]
        if w.get("n_splits", 0) == 0 or p.get("n_splits", 0) == 0:
            print(f"  {r['sym']}: incomplete (one method produced no splits)")
            continue
        wfa_mean = w["oos_sharpe_mean"]
        pwf_mean = p["oos_sharpe_mean"]
        is_oos_pwf = p["is_oos_corr"]
        delta_pct = (pwf_mean - wfa_mean) / abs(wfa_mean) * 100 if wfa_mean != 0 else float("inf")
        tag = "[STABLE]"
        if abs(pwf_mean - wfa_mean) > 0.3:
            tag = "[DIVERGES]"
        if is_oos_pwf < -0.3:
            tag += " (IS-OOS REVERSED — grid selector ineffective)"
        print(
            f"  {r['sym']:4s}: WFA mean {wfa_mean:+.3f} | PWF mean {pwf_mean:+.3f}"
            f"  (Δ={delta_pct:+.1f}%)  {tag}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
