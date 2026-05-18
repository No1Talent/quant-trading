"""Ensemble WFA on BollReversal/RB with ATR loss cap on 8 RB contracts.

G1 execution: tackles the "alpha exists but optimizer picks wrong + asymmetric
losses" paradox found in BollRev/RB deepdive.

Two interventions, applied jointly:
1. ATR-based hard stop (sl_atr_mult=2.0, atr_window=14) — caps the fat-tail
   losses that drove total OOS return negative
2. Top-3 IS ensemble — instead of trusting the single IS-best params (which
   has -0.60 IS-OOS correlation), pool the top 3 IS combos and average their
   OOS results, smoothing out the "lucky in IS, unlucky in OOS" effect

Cooldown_bars=3 after stop-out prevents immediate re-entry whipsaw in
single-direction trend regimes.

Apples-to-apples comparison vs BollRev/RB 8-contract baseline (commit 7b0f396):
- Same 8 contracts, same 16 folds
- Same boll grid: boll_window [20,30,40] x boll_dev [2.0, 2.5, 3.0]
- Same WFA windows: train=120 / test=60 / step=60
- Same min_trades=10
- Same bt_kwargs (RB: size=10, rate=1e-4, slippage=1)
- Only differences: ensemble selection + ATR stop + cooldown
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

from research.backtest_runner import run_backtest  # noqa: E402
from research.wfa import grid_search, make_windows  # noqa: E402
from research.wfa_rb_batch import BT_KWARGS as RB_BT_KWARGS  # noqa: E402
from research.wfa_rb_batch import CONTRACTS as RB_CONTRACTS  # noqa: E402

PARAM_GRID: dict[str, list[Any]] = {
    "boll_window": [20, 30, 40],
    "boll_dev": [2.0, 2.5, 3.0],
}

# Loss cap fixed at industry-standard levels — not gridded here to keep the
# comparison clean (changes attributable to "loss cap + ensemble", not
# "we found a magic stop value"). Sensitivity test is future work if results
# warrant.
FIXED_LOSS_CAP = {
    "atr_window": 14,
    "sl_atr_mult": 2.0,
    "cooldown_bars": 3,
    "fixed_size": 1,
}
# G1 tested 3 variants. Change FIXED_LOSS_CAP and re-run to reproduce:
#   - sl_atr_mult=0.0, cooldown=0 → "ensemble only" (no loss cap)
#   - sl_atr_mult=2.0, cooldown=3 → "ensemble + tight stop" (this default)
#   - sl_atr_mult=4.0, cooldown=3 → "ensemble + wide stop"
# All three failed to turn total OOS return positive. See research-findings v2 §G1.

TOP_K = 3

# Baseline from single-best 8-contract run (commit 7b0f396)
BASELINE_SINGLE_BEST = {
    "folds": 16,
    "oos_sharpe_mean": 0.264,
    "oos_sharpe_median": 0.859,
    "oos_positive_pct": 62.5,
    "is_oos_corr": -0.595,
    "total_oos_return_pct": -0.481,
    "oos_sharpe_min": -4.712,
    "oos_sharpe_max": 3.541,
}


def ensemble_fold(
    strategy_class: type,
    vt_symbol: str,
    interval: str,
    train_start: datetime,
    train_end: datetime,
    test_start: datetime,
    test_end: datetime,
    min_trades: int,
    bt_kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """One fold: grid on train, take top-K by IS Sharpe, equal-weight OOS average."""
    try:
        _, _, all_results = grid_search(
            strategy_class=strategy_class,
            param_grid=PARAM_GRID,
            fixed_params=FIXED_LOSS_CAP,
            vt_symbol=vt_symbol,
            interval=interval,
            start=train_start,
            end=train_end,
            metric="sharpe_ratio",
            min_trades=min_trades,
            **bt_kwargs,
        )
    except RuntimeError:
        return None

    # Filter to positive-IS-Sharpe candidates with enough trades
    valid = [
        r
        for r in all_results
        if r["stats"].get("sharpe_ratio") is not None
        and r["stats"]["sharpe_ratio"] > 0
        and r["stats"].get("total_trade_count", 0) >= min_trades
    ]
    if not valid:
        return None

    valid.sort(key=lambda r: r["stats"]["sharpe_ratio"], reverse=True)
    top = valid[:TOP_K]

    # Run OOS for each member
    oos_rows = []
    for entry in top:
        params = {**FIXED_LOSS_CAP, **{k: entry["params"][k] for k in PARAM_GRID}}
        oos = run_backtest(
            strategy_class=strategy_class,
            params=params,
            vt_symbol=vt_symbol,
            interval=interval,
            start=test_start,
            end=test_end,
            **bt_kwargs,
        )
        oos_rows.append((params, entry["stats"]["sharpe_ratio"], oos))

    # Equal-weight averages (treat None as 0 for sums, count for averages)
    def avg(key: str) -> float:
        vals = [o.get(key) for _, _, o in oos_rows]
        clean = [v for v in vals if isinstance(v, int | float)]
        return sum(clean) / len(clean) if clean else 0.0

    def total(key: str) -> float:
        return sum(o.get(key) or 0 for _, _, o in oos_rows)

    return {
        "top_k_params": [{k: p[k] for k in PARAM_GRID} for p, _, _ in oos_rows],
        "is_sharpes": [s for _, s, _ in oos_rows],
        "oos_sharpes_individual": [o.get("sharpe_ratio") for _, _, o in oos_rows],
        "oos_returns_individual_pct": [o.get("total_return") for _, _, o in oos_rows],
        "oos_trades_individual": [o.get("total_trade_count") for _, _, o in oos_rows],
        "ens_oos_sharpe": avg("sharpe_ratio"),
        "ens_oos_return_pct": avg("total_return"),
        "ens_oos_trades_total": total("total_trade_count"),
        "ens_oos_max_dd_pct": min(  # worst across members
            (o.get("max_ddpercent") for _, _, o in oos_rows if o.get("max_ddpercent") is not None),
            default=0,
        ),
    }


def main() -> int:
    from strategies.boll_reversal_strategy import BollReversalStrategy

    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 80}\n# Ensemble WFA: BollRev/RB top-{TOP_K} + ATR loss cap\n{'#' * 80}")
    print(f"  Contracts: {[c[0] for c in RB_CONTRACTS]}")
    print(f"  Loss cap fixed: {FIXED_LOSS_CAP}")
    print(f"  Grid: {PARAM_GRID}")

    rows = []
    for vt_symbol, start, end in RB_CONTRACTS:
        windows = make_windows(start, end, train_days=120, test_days=60, step_days=60)
        for i, w in enumerate(windows, 1):
            print(
                f"  [{vt_symbol}] fold {i}: train {w.train_start.date()}->{w.train_end.date()} test {w.test_start.date()}->{w.test_end.date()}"
            )
            ens = ensemble_fold(
                strategy_class=BollReversalStrategy,
                vt_symbol=vt_symbol,
                interval="1h",
                train_start=w.train_start,
                train_end=w.train_end,
                test_start=w.test_start,
                test_end=w.test_end,
                min_trades=10,
                bt_kwargs=RB_BT_KWARGS,
            )
            if ens is None:
                print("    SKIPPED (no valid IS combos)")
                continue
            rows.append(
                {
                    "contract": vt_symbol,
                    "fold": i,
                    "train_end": w.train_end.date(),
                    "test_end": w.test_end.date(),
                    "top_k_params": ens["top_k_params"],
                    "is_sharpes": [round(s, 3) for s in ens["is_sharpes"]],
                    "oos_sharpes_indiv": [
                        round(s, 3) if s is not None else None
                        for s in ens["oos_sharpes_individual"]
                    ],
                    "oos_returns_indiv_pct": [
                        round(r, 3) if r is not None else None
                        for r in ens["oos_returns_individual_pct"]
                    ],
                    "ens_oos_sharpe": ens["ens_oos_sharpe"],
                    "ens_oos_return_pct": ens["ens_oos_return_pct"],
                    "ens_oos_trades": ens["ens_oos_trades_total"],
                    "ens_oos_max_dd_pct": ens["ens_oos_max_dd_pct"],
                }
            )

    if not rows:
        print("Ensemble produced no folds.")
        return 1

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 30)

    print(
        f"\n{'=' * 110}\nALL FOLDS (ensemble of top-{TOP_K} IS combos, per-fold equal-weight OOS)\n{'=' * 110}"
    )
    summary_cols = [
        "contract",
        "fold",
        "train_end",
        "test_end",
        "ens_oos_sharpe",
        "ens_oos_return_pct",
        "ens_oos_trades",
        "ens_oos_max_dd_pct",
    ]
    print(df[summary_cols].to_string(index=False))

    oos_sharpe = df["ens_oos_sharpe"]
    oos_return = df["ens_oos_return_pct"]
    pos_pct = (oos_sharpe > 0).mean() * 100

    print(f"\n{'=' * 110}\nENSEMBLE vs SINGLE-BEST (8-contract baseline)\n{'=' * 110}")
    cmp_rows = [
        ("Folds", len(df), BASELINE_SINGLE_BEST["folds"]),
        ("OOS Sharpe mean", oos_sharpe.mean(), BASELINE_SINGLE_BEST["oos_sharpe_mean"]),
        ("OOS Sharpe median", oos_sharpe.median(), BASELINE_SINGLE_BEST["oos_sharpe_median"]),
        ("OOS positive %", pos_pct, BASELINE_SINGLE_BEST["oos_positive_pct"]),
        ("Total OOS return %", oos_return.sum(), BASELINE_SINGLE_BEST["total_oos_return_pct"]),
        ("OOS Sharpe min", oos_sharpe.min(), BASELINE_SINGLE_BEST["oos_sharpe_min"]),
        ("OOS Sharpe max", oos_sharpe.max(), BASELINE_SINGLE_BEST["oos_sharpe_max"]),
    ]
    print(f"  {'Metric':22s} {'Ensemble+SL':>14s} {'Single-best':>14s} {'Δ':>12s}")
    print("  " + "-" * 66)
    for name, v_new, v_old in cmp_rows:
        delta = v_new - v_old
        print(f"  {name:22s} {v_new:>+14.3f} {v_old:>+14.3f} {delta:>+12.3f}")

    print(f"\n{'=' * 110}\nPER-CONTRACT ENSEMBLE OOS\n{'=' * 110}")
    per_ct = (
        df.groupby("contract")
        .agg(
            folds=("fold", "count"),
            ens_sharpe_mean=("ens_oos_sharpe", "mean"),
            ens_sharpe_min=("ens_oos_sharpe", "min"),
            ens_return_sum=("ens_oos_return_pct", "sum"),
            ens_positive=("ens_oos_sharpe", lambda s: (s > 0).sum()),
        )
        .round(3)
    )
    print(per_ct.to_string())

    print(f"\n{'=' * 110}\nVERDICT\n{'=' * 110}")
    total_return = oos_return.sum()
    if total_return > 0.3 and oos_sharpe.mean() > 0.2:
        print(f"  [WIN] Total OOS return turned POSITIVE ({total_return:+.3f}%, was -0.48%).")
        print("        Ensemble + loss cap rescued BollRev/RB into a real candidate.")
        print("        Next: cost sensitivity, paper trading, real money sizing study.")
    elif total_return > 0:
        print(
            f"  [MARGINAL] Total OOS return {total_return:+.3f}% — broke into positive but small."
        )
        print("             Real but not yet capital-worthy. Try wider loss cap sweep.")
    elif total_return > BASELINE_SINGLE_BEST["total_oos_return_pct"]:
        print(f"  [IMPROVED] Total OOS return {total_return:+.3f}% — improvement over -0.48%")
        print("             baseline but still negative. Loss cap helped but didn't")
        print("             close the gap. Try sl_atr_mult sweep or different exit logic.")
    else:
        print(f"  [WORSE] Total OOS return {total_return:+.3f}% — worse than -0.48% baseline.")
        print("          Loss cap is hurting more than helping (whipsaw losses). Reconsider.")

    out_path = REPO_ROOT / "research" / "wfa_results_rb_boll_ensemble.csv"
    df.to_csv(out_path, index=False)
    print(f"\nFull fold table → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
