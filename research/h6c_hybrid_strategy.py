"""H6c: hybrid DoubleMa-direction × post-rollover gate WFA on I.

Tests the trend×rollover-intersection hypothesis. H6a proved I.raw PnL
concentrates in the ±5-day rollover band (60.4% PnL share, 11.5% calendar
share); H6b proved naive `sign(gap)` carry no longer works post-2018
(+0.053 OOS). H6c asks: does the MA cross direction — which integrates
pre-roll info and adapts to regime shifts — capture the rollover-window
edge when gap-sign alone can't?

Strategy: `MaCrossRolloverGatedStrategy` — DoubleMa logic but new entries
only fire within `post_roll_window` trading days AFTER a detected rollover.
Exits on opposite cross always fire (no orphan positions).

WFA: same I config as H2/H4/H6a — start 2013-10-18, train=700/test=250/
step=250, BT = {capital=1M, rate=6e-5, slippage=0.5, size=100, pricetick=0.5}.

Grid: fast={10,20,30} × slow={40,60,100} × post_roll_window={3,5,10}
  = 27 combos per fold. Slow capped at 100 per the documented vn.py
  load_bar quirk.

Verdict thresholds (vs I.raw DoubleMa baseline +0.445):
  Sharpe >= +0.55 AND pos% >= 70  → [HYBRID_BEATS]    — replace I leg in H4
  Sharpe in [+0.35, +0.55)        → [HYBRID_MATCHES]  — same edge, cleaner
                                                         mechanism story
  Sharpe in [+0.10, +0.35)        → [HYBRID_FILTERS]  — gating extracted a
                                                         signal but removed
                                                         too much; DoubleMa
                                                         is better undiluted
  Sharpe < +0.10                  → [HYBRID_FAILS]    — gating destroys
                                                         the edge; trend×roll
                                                         intersection isn't
                                                         where the alpha lives
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

GRID = {
    "fast_window": [10, 20, 30],
    "slow_window": [40, 60, 100],
    "post_roll_window": [3, 5, 10],
}
FIXED_PARAMS = {
    "oi_pct_threshold": 20.0,
    "gap_floor_pct": 0.3,
    "fixed_size": 1,
}

TRAIN_DAYS = 700
TEST_DAYS = 250
STEP_DAYS = 250
MIN_TRADES = 2  # gated strategy → cross × ±K-day rollover band is very sparse
SKIP_EMPTY_FOLDS = True  # early DCE-I folds may have too few rollovers

I_RAW_DOUBLEMA = {
    "folds": 15,
    "oos_sharpe_mean": 0.445,
    "oos_positive_pct": 73.3,
    "is_oos_corr": 0.04,
    "total_oos_return_pct": 7.92,
}
I_RAW_CARRYROLL = {
    "folds": 15,
    "oos_sharpe_mean": 0.053,
    "oos_positive_pct": 40.0,
    "is_oos_corr": -0.254,
    "total_oos_return_pct": -0.94,
}


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 80}\n# H6c: MA-cross × post-rollover gate WFA on I\n{'#' * 80}")
    print(f"  Symbol: {VT_SYMBOL}  {START.date()} → {END.date()}")
    print(
        f"  Grid: fast={GRID['fast_window']} × slow={GRID['slow_window']} × "
        f"post_roll_window={GRID['post_roll_window']}"
    )
    print(
        f"  Fixed: oi_pct={FIXED_PARAMS['oi_pct_threshold']}%, "
        f"gap={FIXED_PARAMS['gap_floor_pct']}%"
    )
    print(
        f"  Baselines: I.raw DoubleMa Sharpe={I_RAW_DOUBLEMA['oos_sharpe_mean']:+.3f}, "
        f"I.raw CarryRoll Sharpe={I_RAW_CARRYROLL['oos_sharpe_mean']:+.3f}"
    )

    from research.wfa import run_wfa
    from strategies.ma_cross_rollover_gated_strategy import MaCrossRolloverGatedStrategy

    print("\n--- Running WFA on MaCrossRolloverGatedStrategy ---")
    df = run_wfa(
        strategy_class=MaCrossRolloverGatedStrategy,
        param_grid=GRID,
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
        skip_empty_folds=SKIP_EMPTY_FOLDS,
        **I_BT,
    )

    if len(df) == 0:
        print("ERROR: WFA returned zero folds — likely no fold met min_trades.")
        return 1

    oos = df["oos_sharpe"].dropna()
    n_folds = len(df)
    s_mean = float(oos.mean())
    s_median = float(oos.median())
    pos_pct = float((oos > 0).mean() * 100)
    is_oos_corr = float(df[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1])
    total_ret = float(df["oos_return_pct"].sum())
    mean_trades = float(df["oos_trades"].mean()) if "oos_trades" in df else float("nan")

    # Selection distribution
    fast_picks: dict[int, int] = {}
    slow_picks: dict[int, int] = {}
    prw_picks: dict[int, int] = {}
    for params in df["best_params"]:
        if not isinstance(params, dict):
            continue
        f, s, p = (
            params.get("fast_window"),
            params.get("slow_window"),
            params.get("post_roll_window"),
        )
        if f is not None:
            fast_picks[f] = fast_picks.get(f, 0) + 1
        if s is not None:
            slow_picks[s] = slow_picks.get(s, 0) + 1
        if p is not None:
            prw_picks[p] = prw_picks.get(p, 0) + 1

    print(f"\n  WFA: {n_folds} folds")
    print(
        f"  OOS Sharpe mean    : {s_mean:+.3f}  "
        f"(DM baseline: {I_RAW_DOUBLEMA['oos_sharpe_mean']:+.3f}, "
        f"Carry: {I_RAW_CARRYROLL['oos_sharpe_mean']:+.3f})"
    )
    print(f"  OOS Sharpe median  : {s_median:+.3f}")
    print(
        f"  OOS positive %     : {pos_pct:.1f}  (DM: {I_RAW_DOUBLEMA['oos_positive_pct']:.1f}, "
        f"Carry: {I_RAW_CARRYROLL['oos_positive_pct']:.1f})"
    )
    print(
        f"  IS-OOS Sharpe corr : {is_oos_corr:+.3f}  (DM: {I_RAW_DOUBLEMA['is_oos_corr']:+.3f}, "
        f"Carry: {I_RAW_CARRYROLL['is_oos_corr']:+.3f})"
    )
    print(
        f"  Total OOS return % : {total_ret:+.2f}  (DM: {I_RAW_DOUBLEMA['total_oos_return_pct']:+.2f}, "
        f"Carry: {I_RAW_CARRYROLL['total_oos_return_pct']:+.2f})"
    )
    print(f"  Mean OOS trades    : {mean_trades:.1f} per fold")
    print(f"  Fast winners       : {dict(sorted(fast_picks.items()))}")
    print(f"  Slow winners       : {dict(sorted(slow_picks.items()))}")
    print(f"  post_roll winners  : {dict(sorted(prw_picks.items()))}")

    # Per-fold detail
    print(f"\n{'=' * 80}\nPer-fold detail\n{'=' * 80}")
    print(
        f"  {'Fold':>4} {'Train→Test':>26s} {'best':>14s} {'IS S':>7s} "
        f"{'OOS S':>7s} {'OOS %':>7s} {'OOS#':>5s}"
    )
    print("  " + "-" * 82)
    for _, r in df.iterrows():
        bp = r["best_params"] if isinstance(r["best_params"], dict) else {}
        bp_str = f"f{bp.get('fast_window','?')}/s{bp.get('slow_window','?')}/p{bp.get('post_roll_window','?')}"
        print(
            f"  {int(r['fold']):>4d} {str(r['train_start']) + '→' + str(r['test_end']):>26s} "
            f"{bp_str:>14s} {float(r['is_sharpe']):>+7.2f} {float(r['oos_sharpe']):>+7.2f} "
            f"{float(r['oos_return_pct']):>+7.2f} {int(r['oos_trades']):>5d}"
        )

    # Verdict
    print(f"\n{'=' * 80}\nH6c VERDICT\n{'=' * 80}")
    if s_mean >= 0.55 and pos_pct >= 70:
        verdict = "[HYBRID_BEATS]"
        msg = (
            "Hybrid beats raw DoubleMa: trend × rollover intersection is the\n"
            "  alpha source. The gating amplifies the signal beyond either factor\n"
            "  alone. Action: swap I leg in H4 ensemble; re-run h4_ensemble.py\n"
            "  with MaCrossRolloverGated in place of DoubleMa on I.raw."
        )
    elif s_mean >= 0.35 and pos_pct >= 55:
        verdict = "[HYBRID_MATCHES]"
        msg = (
            "Hybrid matches raw DoubleMa Sharpe with a cleaner mechanism story\n"
            "  (trend direction × rollover timing). No quantitative lift but\n"
            "  swapping for narrative clarity is defensible. Consider H7 ensemble\n"
            "  swap and check whether correlation to AG/CU legs changes."
        )
    elif s_mean >= 0.10:
        verdict = "[HYBRID_FILTERS]"
        msg = (
            f"Hybrid Sharpe {s_mean:+.3f} sits between Carry ({I_RAW_CARRYROLL['oos_sharpe_mean']:+.3f})\n"
            "  and DoubleMa (+0.445). Gating extracts real signal but removes too\n"
            "  much — DoubleMa undiluted is better. The far-from-roll trades\n"
            "  DoubleMa makes ARE adding value despite the H6a per-day attribution\n"
            "  suggesting they shouldn't. Possible explanation: those FAR trades\n"
            "  initiate positions held INTO rollover events, capturing the carry\n"
            "  amortised over many bars rather than on the roll-day itself.\n"
            "  Keep I.raw + DoubleMa in H4 ensemble."
        )
    else:
        verdict = "[HYBRID_FAILS]"
        msg = (
            f"Hybrid Sharpe {s_mean:+.3f} ≈ Carry-only Sharpe — gating to post-roll\n"
            "  window destroys the edge entirely. The MA cross direction works on I\n"
            "  ONLY when applied to the full daily series; isolating it to rollover\n"
            "  windows removes the cross signal's predictive power. Trend×rollover\n"
            "  is not the right factorisation. Keep I.raw + DoubleMa in H4 ensemble."
        )
    print(f"  {verdict}\n  {msg}")

    # Save artefacts
    out_dir = REPO_ROOT / "research"
    df.to_csv(out_dir / "wfa_results_h6c_hybrid.csv", index=False)
    summary_path = out_dir / "h6c_summary.csv"
    pd.DataFrame(
        [
            {
                "strategy": "I.raw_doubleMa",
                "folds": I_RAW_DOUBLEMA["folds"],
                "oos_sharpe_mean": I_RAW_DOUBLEMA["oos_sharpe_mean"],
                "oos_positive_pct": I_RAW_DOUBLEMA["oos_positive_pct"],
                "is_oos_corr": I_RAW_DOUBLEMA["is_oos_corr"],
                "total_oos_return_pct": I_RAW_DOUBLEMA["total_oos_return_pct"],
            },
            {
                "strategy": "I.raw_carryRoll",
                "folds": I_RAW_CARRYROLL["folds"],
                "oos_sharpe_mean": I_RAW_CARRYROLL["oos_sharpe_mean"],
                "oos_positive_pct": I_RAW_CARRYROLL["oos_positive_pct"],
                "is_oos_corr": I_RAW_CARRYROLL["is_oos_corr"],
                "total_oos_return_pct": I_RAW_CARRYROLL["total_oos_return_pct"],
            },
            {
                "strategy": "I.raw_maCrossRollGated",
                "folds": n_folds,
                "oos_sharpe_mean": s_mean,
                "oos_sharpe_median": s_median,
                "oos_positive_pct": pos_pct,
                "is_oos_corr": is_oos_corr,
                "total_oos_return_pct": total_ret,
                "mean_oos_trades": mean_trades,
                "fast_winners": str(dict(sorted(fast_picks.items()))),
                "slow_winners": str(dict(sorted(slow_picks.items()))),
                "post_roll_winners": str(dict(sorted(prw_picks.items()))),
                "verdict": verdict,
            },
        ]
    ).to_csv(summary_path, index=False)
    print(f"\nFull fold table → {out_dir / 'wfa_results_h6c_hybrid.csv'}")
    print(f"Summary table   → {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
