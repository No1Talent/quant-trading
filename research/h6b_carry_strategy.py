"""H6b: WFA of explicit CarryRollStrategy on I (iron ore) vs I.raw baseline.

Background — H6a attribution proved I's +0.445 Sharpe is 60.4%
concentrated in ±5 trading days of H1.5 rollovers (5.27x concentration,
ROLL_pm1 bucket Sharpe +4.64, FAR Sharpe +0.04 ≈ I.adj15r's +0.048).
DoubleMa is accidentally a carry strategy on I.

This script tests an EXPLICIT carry strategy: enter in direction of
rollover gap, hold for `hold_days`, exit. Same WFA windows / bt config
as I.raw so numbers are directly comparable.

Success criteria:
  [CARRY_BEATS]         Sharpe ≥ +0.50 AND pos% ≥ 60 → new H4 leg
  [CARRY_MATCHES]       Sharpe ∈ [+0.35, +0.50)      → cleaner story, keep
  [CARRY_UNDERPERFORMS] Sharpe < +0.35                → DoubleMa did more
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

VT_SYMBOL = "i_continuous.DCE"
START = datetime(2013, 10, 18)
END = datetime(2026, 5, 15)
I_BT: dict[str, Any] = dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=100, pricetick=0.5)

# Grid: how many bars to hold the carry position. Spans the H6a buckets:
#   3 days → captures ROLL_pm1+pm2 only
#   5 days → captures ROLL_pm1+pm2..5 (the +75% PnL band)
#   7,10  → also includes some of pm6..10
#   15    → starts bleeding into FAR — should underperform
HOLD_GRID = {"hold_days": [3, 5, 7, 10, 15]}
FIXED_PARAMS = {"oi_pct_threshold": 20.0, "gap_floor_pct": 0.3, "fixed_size": 1}

TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
MIN_TRADES = 3  # carry fires ~4 trades/year; lower bar than DoubleMa

# I.raw DoubleMa baseline (from h6_carry_attribution.py / h4_ensemble.py)
I_RAW_BASELINE = {
    "folds": 15,
    "oos_sharpe_mean": 0.445,
    "oos_sharpe_median": None,  # not memoized; computed if needed from h2_followup CSV
    "oos_positive_pct": 73.3,
    "is_oos_corr": 0.04,
    "total_oos_return_pct": 7.92,
}


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 80}\n# H6b: explicit CarryRollStrategy WFA on I vs I.raw +0.445\n{'#' * 80}")
    print(f"  Symbol: {VT_SYMBOL}  {START.date()} → {END.date()}")
    print(f"  Hold-days grid: {HOLD_GRID['hold_days']}")
    print(
        f"  Fixed: oi_pct={FIXED_PARAMS['oi_pct_threshold']}%, gap={FIXED_PARAMS['gap_floor_pct']}%"
    )

    from research.wfa import run_wfa
    from strategies.carry_roll_strategy import CarryRollStrategy

    print("\n--- Running WFA on CarryRollStrategy ---")
    df = run_wfa(
        strategy_class=CarryRollStrategy,
        param_grid=HOLD_GRID,
        fixed_params=FIXED_PARAMS,
        vt_symbol=VT_SYMBOL,
        interval="1d",
        start=START,
        end=END,
        train_days=TRAIN_DAYS,
        test_days=TEST_DAYS,
        step_days=STEP_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        **I_BT,
    )

    if len(df) == 0:
        print("ERROR: WFA returned zero folds — likely no fold met min_trades=3.")
        return 1

    oos = df["oos_sharpe"].dropna()
    n_folds = len(df)
    s_mean = float(oos.mean())
    s_median = float(oos.median())
    pos_pct = float((oos > 0).mean() * 100)
    is_oos_corr = float(df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1])
    total_ret = float(df["oos_return_pct"].sum())
    mean_trades = float(df["oos_trades"].mean()) if "oos_trades" in df else float("nan")

    # Hold-days selection distribution
    hold_choices: dict[int, int] = {}
    for params in df["best_params"]:
        hd = params.get("hold_days") if isinstance(params, dict) else None
        if hd is not None:
            hold_choices[hd] = hold_choices.get(hd, 0) + 1

    print(f"\n  WFA: {n_folds} folds")
    print(
        f"  OOS Sharpe mean   : {s_mean:+.3f}  (baseline I.raw: {I_RAW_BASELINE['oos_sharpe_mean']:+.3f})"
    )
    print(f"  OOS Sharpe median : {s_median:+.3f}")
    print(
        f"  OOS positive %    : {pos_pct:.1f}  (baseline: {I_RAW_BASELINE['oos_positive_pct']:.1f})"
    )
    print(
        f"  IS-OOS Sharpe corr: {is_oos_corr:+.3f}  (baseline: {I_RAW_BASELINE['is_oos_corr']:+.3f})"
    )
    print(
        f"  Total OOS return %: {total_ret:+.2f}  (baseline: {I_RAW_BASELINE['total_oos_return_pct']:+.2f})"
    )
    print(f"  Mean OOS trades   : {mean_trades:.1f} per fold")
    print(f"  Hold-days winners : {dict(sorted(hold_choices.items()))}")

    # Per-fold detail
    print(f"\n{'=' * 80}\nPer-fold detail\n{'=' * 80}")
    print(
        f"  {'Fold':>4} {'Train→Test':>26s} {'best hd':>8s} {'IS S':>7s} {'OOS S':>7s} {'OOS %':>7s} {'OOS#':>5s}"
    )
    print("  " + "-" * 76)
    for _, r in df.iterrows():
        bp = r["best_params"]
        hd = bp.get("hold_days") if isinstance(bp, dict) else "?"
        print(
            f"  {int(r['fold']):>4d} {str(r['train_start']) + '→' + str(r['test_end']):>26s} "
            f"{str(hd):>8s} {float(r['is_sharpe']):>+7.2f} {float(r['oos_sharpe']):>+7.2f} "
            f"{float(r['oos_return_pct']):>+7.2f} {int(r['oos_trades']):>5d}"
        )

    # Verdict
    print(f"\n{'=' * 80}\nH6b VERDICT\n{'=' * 80}")
    if s_mean >= 0.50 and pos_pct >= 60:
        verdict = "[CARRY_BEATS]"
        msg = (
            "Explicit carry strategy on I beats DoubleMa-on-raw at Sharpe >= +0.50\n"
            "  and pos% >= 60. This becomes the new I leg in H4 ensemble.\n"
            "  Next: H7 — swap I.raw → I.carry, re-run h4_ensemble.py."
        )
    elif s_mean >= 0.35 and pos_pct >= 55:
        verdict = "[CARRY_MATCHES]"
        msg = (
            "Carry strategy matches DoubleMa-on-raw with cleaner mechanism.\n"
            "  No Sharpe lift but a much more interpretable factor story:\n"
            "  trend on AG/CU + carry on I. Worth swapping into H4 ensemble (H7)\n"
            "  for narrative clarity even without quantitative improvement."
        )
    else:
        verdict = "[CARRY_UNDERPERFORMS]"
        msg = (
            f"Carry strategy Sharpe {s_mean:+.3f} < +0.35 — DoubleMa-on-raw is doing more\n"
            "  than just carry capture. The H6a attribution showed 60% of PnL is\n"
            "  carry-adjacent, but the remaining 40% (including some pre-roll days\n"
            "  and the carry-driven momentum that the slow MA confirms) carries\n"
            "  the headline Sharpe. Keep I.raw + DoubleMa in H4 ensemble.\n"
            "  Investigate: what's in the ROLL_pm6..10 / pre-roll bands that pure\n"
            "  same-day rollover detection misses?"
        )
    print(f"  {verdict}\n  {msg}")

    # Save artefacts
    out_dir = REPO_ROOT / "research"
    df.to_csv(out_dir / "wfa_results_h6b_carry.csv", index=False)
    summary_path = out_dir / "h6b_summary.csv"
    pd.DataFrame(
        [
            {
                "strategy": "I.raw_doubleMa",
                "folds": I_RAW_BASELINE["folds"],
                "oos_sharpe_mean": I_RAW_BASELINE["oos_sharpe_mean"],
                "oos_positive_pct": I_RAW_BASELINE["oos_positive_pct"],
                "is_oos_corr": I_RAW_BASELINE["is_oos_corr"],
                "total_oos_return_pct": I_RAW_BASELINE["total_oos_return_pct"],
            },
            {
                "strategy": "I.raw_carryRoll",
                "folds": n_folds,
                "oos_sharpe_mean": s_mean,
                "oos_sharpe_median": s_median,
                "oos_positive_pct": pos_pct,
                "is_oos_corr": is_oos_corr,
                "total_oos_return_pct": total_ret,
                "mean_oos_trades": mean_trades,
                "hold_days_winners": str(dict(sorted(hold_choices.items()))),
                "verdict": verdict,
            },
        ]
    ).to_csv(summary_path, index=False)
    print(f"\nFull fold table → {out_dir / 'wfa_results_h6b_carry.csv'}")
    print(f"Summary table   → {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
