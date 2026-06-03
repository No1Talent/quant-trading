"""M3.6: Deflate the H4 ensemble Sharpe for selection bias and non-normality.

M3.5 (research/m35_h4_ensemble_cpcv.py) established that the H4 portfolio
Sharpe of +0.993 (WFA) falls to +0.526 under Purged Walk-Forward. That is the
*generalisation* lens. This script adds the *selection-bias* lens the CPCV
machinery was built to support but never emitted:

  PSR(0)  — P(true Sharpe > 0) given sample length + skew + kurtosis.
            Series-only; computed on the saved M3.5 PWF portfolio panel.
  MinTRL  — observations needed before the Sharpe is significant at 95%.
            Compare against the panel length to see if we are already there.
  DSR     — Deflated Sharpe Ratio. For each instrument we re-run all 9 DoubleMa
            grid configs over the full sample, take the best in-sample Sharpe,
            and deflate it for having searched 9 trials with the observed
            cross-trial Sharpe dispersion. This is the classic "I optimised
            over the whole sample, is best-of-9 better than the luckiest of 9
            coin flips?" question.
  PBO     — Probability of Backtest Overfitting via CSCV on the same 9-config
            daily-PnL matrix: how often the in-sample-best config lands below
            the out-of-sample median.

DSR/PBO complement, not replace, the PWF OOS numbers from M3/M3.5: PWF asks
"does the *selected* strategy hold up out of sample?"; DSR/PBO ask "is the
selection itself distinguishable from luck given the grid size?".

Outputs:
  - research/m36_overfit_stats.csv   one row per instrument + portfolio
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from research.overfit_stats import (  # noqa: E402
    deflated_sharpe_ratio,
    min_track_record_length,
    pbo_cscv,
    probabilistic_sharpe_ratio,
    sharpe_skew_kurt,
)

# Identical spec to m35_h4_ensemble_cpcv.py / m3_h4_cpcv.py — keep apples-to-apples.
INSTRUMENTS: list[dict[str, Any]] = [
    {
        "sym": "AG",
        "vt_symbol": "ag_continuous_adj15.SHFE",
        "start": datetime(2012, 5, 10),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=1, size=15, pricetick=1),
        "pwf_oos_sharpe": 0.630,  # from project_research_layer2_status v14 (M3.5)
    },
    {
        "sym": "I",
        "vt_symbol": "i_continuous.DCE",
        "start": datetime(2013, 10, 18),
        "bt": dict(capital=1_000_000, rate=6e-5, slippage=0.5, size=100, pricetick=0.5),
        "pwf_oos_sharpe": -0.266,
    },
    {
        "sym": "CU",
        "vt_symbol": "cu_continuous_adj15.SHFE",
        "start": datetime(2005, 1, 4),
        "bt": dict(capital=1_000_000, rate=5e-5, slippage=10, size=5, pricetick=10),
        "pwf_oos_sharpe": 0.140,
    },
]

END_DATE = datetime(2026, 5, 15)
DM_GRID = {"fast_window": [10, 20, 30], "slow_window": [40, 60, 100]}
PANEL_CSV = REPO_ROOT / "research" / "m35_h4_pwf_panel.csv"
N_PARTITIONS = 16  # CSCV blocks → C(16,8)=12,870 combinations
TRADING_DAYS = 252.0


def _annualize(sr_per_period: float) -> float:
    return sr_per_period * np.sqrt(TRADING_DAYS)


def build_trial_matrix(inst: dict) -> dict:
    """Run all 9 DoubleMa grid configs over the full sample, capturing daily
    net_pnl. Returns the aligned (T × 9) matrix plus per-config Sharpes."""
    from research.backtest_runner import run_backtest
    from strategies.double_ma_strategy import DoubleMaStrategy

    combos = [dict(zip(DM_GRID, vals)) for vals in product(*DM_GRID.values())]
    series: dict[str, pd.Series] = {}
    for combo in combos:
        label = f"f{combo['fast_window']}_s{combo['slow_window']}"
        _, daily = run_backtest(
            strategy_class=DoubleMaStrategy,
            params={"fixed_size": 1, **combo},
            vt_symbol=inst["vt_symbol"],
            interval="1d",
            start=inst["start"],
            end=END_DATE,
            return_daily_df=True,
            **inst["bt"],
        )
        if daily is None or len(daily) == 0:
            s = pd.Series(dtype=float)
        else:
            s = daily["net_pnl"].copy()
            s.index = pd.to_datetime(s.index)
        series[label] = s

    # Align on the union of trading days; a flat day contributes 0 PnL.
    aligned = pd.DataFrame(series).sort_index().fillna(0.0)
    per_config = []
    for col in aligned.columns:
        sr, skew, kurt, n = sharpe_skew_kurt(aligned[col].values)
        per_config.append({"config": col, "sr": sr, "skew": skew, "kurt": kurt, "n": n})
    return {"sym": inst["sym"], "matrix": aligned, "per_config": pd.DataFrame(per_config)}


def deflate_instrument(inst: dict, trial: dict) -> dict:
    """DSR (best-of-9 in-sample) + PBO (CSCV) for one instrument."""
    pc = trial["per_config"].dropna(subset=["sr"]).reset_index(drop=True)
    best = pc.loc[pc["sr"].idxmax()]
    srs = pc["sr"].to_numpy()
    trial_var = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0
    n_trials = int(len(srs))

    dsr = deflated_sharpe_ratio(
        sr=float(best["sr"]),
        n_obs=int(best["n"]),
        skew=float(best["skew"]),
        kurt=float(best["kurt"]),
        n_trials=n_trials,
        trial_sr_variance=trial_var,
    )
    pbo = pbo_cscv(trial["matrix"].to_numpy(), n_partitions=N_PARTITIONS)

    return {
        "scope": inst["sym"],
        "sr_per_period": float(best["sr"]),
        "sr_annual": _annualize(float(best["sr"])),
        "best_config": str(best["config"]),
        "pwf_oos_sharpe": inst["pwf_oos_sharpe"],
        "n_obs": int(best["n"]),
        "skew": float(best["skew"]),
        "kurt": float(best["kurt"]),
        "n_trials": n_trials,
        "trial_sr_variance": trial_var,
        "sr0_per_period": dsr.sr0,
        "sr0_annual": _annualize(dsr.sr0),
        "psr_zero": dsr.psr_zero,
        "dsr": dsr.dsr,
        "pbo": pbo.pbo,
        "pbo_logit_mean": pbo.logit_mean,
        "pbo_n_combos": pbo.n_combos,
        "min_trl_95": min_track_record_length(
            float(best["sr"]), float(best["skew"]), float(best["kurt"]), 0.0, 0.95
        ),
    }


def deflate_portfolio(pooled_trial_var: float, pooled_n_trials: int) -> list[dict]:
    """PSR(0) + MinTRL on the saved M3.5 PWF portfolio series, plus an
    assumption-labelled DSR sweep over candidate trial counts (the portfolio is
    not itself grid-selected, so N_trials is a judgement call — we show the
    sensitivity rather than assert one number)."""
    panel = pd.read_csv(PANEL_CSV, index_col=0, parse_dates=True)
    portfolio = panel.sum(axis=1)
    sr, skew, kurt, n = sharpe_skew_kurt(portfolio.values)

    base = {
        "scope": "PORTFOLIO",
        "sr_per_period": sr,
        "sr_annual": _annualize(sr),
        "best_config": "ensemble(AG+I+CU)",
        "pwf_oos_sharpe": _annualize(sr),  # this IS the PWF portfolio number
        "n_obs": n,
        "skew": skew,
        "kurt": kurt,
        "psr_zero": probabilistic_sharpe_ratio(sr, n, skew, kurt, 0.0),
        "min_trl_95": min_track_record_length(sr, skew, kurt, 0.0, 0.95),
    }

    rows = []
    # Portfolio DSR is assumption-dependent: show N_trials ∈ {9, 27, pooled}.
    for n_trials in sorted({9, 27, pooled_n_trials}):
        dsr = deflated_sharpe_ratio(sr, n, skew, kurt, n_trials, pooled_trial_var)
        rows.append(
            {
                **base,
                "n_trials": n_trials,
                "trial_sr_variance": pooled_trial_var,
                "sr0_per_period": dsr.sr0,
                "sr0_annual": _annualize(dsr.sr0),
                "dsr": dsr.dsr,
                "pbo": float("nan"),  # PBO is a per-grid construct, not portfolio-level
                "pbo_logit_mean": float("nan"),
                "pbo_n_combos": 0,
            }
        )
    return rows


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctabacktester.backtesting").setLevel(logging.ERROR)
    logging.getLogger("backtest_runner").setLevel(logging.WARNING)

    if not PANEL_CSV.exists():
        print(f"ERROR: {PANEL_CSV} not found — run m35_h4_ensemble_cpcv.py first.")
        return 1

    print(f"\n{'#' * 90}")
    print("# M3.6 — Deflating the H4 ensemble Sharpe (selection bias + non-normality)")
    print(f"# grid={DM_GRID}  partitions(CSCV)={N_PARTITIONS}")
    print(f"{'#' * 90}")

    # --- Per-instrument: live re-run for trial matrix → DSR + PBO ---
    inst_rows: list[dict] = []
    all_trial_srs: list[float] = []
    for inst in INSTRUMENTS:
        print(f"\n--- {inst['sym']}: running 9 grid configs over full sample ---")
        trial = build_trial_matrix(inst)
        all_trial_srs.extend(trial["per_config"]["sr"].dropna().tolist())
        row = deflate_instrument(inst, trial)
        inst_rows.append(row)
        print(
            f"  best-of-9 IS Sharpe={row['sr_annual']:+.3f} (ann, {row['best_config']})  "
            f"| PWF OOS={row['pwf_oos_sharpe']:+.3f}"
        )
        print(
            f"  SR0(null max of 9)={row['sr0_annual']:+.3f} ann  "
            f"PSR(0)={row['psr_zero']:.3f}  DSR={row['dsr']:.3f}  PBO={row['pbo']:.3f}"
        )

    # --- Portfolio: PSR/MinTRL on saved series + assumption-labelled DSR ---
    pooled_var = float(np.var(all_trial_srs, ddof=1)) if len(all_trial_srs) > 1 else 0.0
    port_rows = deflate_portfolio(pooled_var, pooled_n_trials=len(all_trial_srs))

    all_rows = inst_rows + port_rows
    df = pd.DataFrame(all_rows)
    out = REPO_ROOT / "research" / "m36_overfit_stats.csv"
    df.to_csv(out, index=False)

    # --- Report ---
    print(f"\n{'=' * 90}")
    print("PER-INSTRUMENT (DSR deflates best-of-9 in-sample Sharpe; PBO on 9-config matrix)")
    print(f"{'=' * 90}")
    print(
        f"  {'sym':4s} {'IS_best':>8s} {'SR0':>7s} {'PWF_OOS':>8s} "
        f"{'PSR(0)':>7s} {'DSR':>6s} {'PBO':>6s} {'MinTRL':>8s} {'n':>6s}"
    )
    for r in inst_rows:
        trl = r["min_trl_95"]
        trl_s = "inf" if not np.isfinite(trl) else f"{trl:,.0f}"
        flag = "" if np.isfinite(trl) and trl <= r["n_obs"] else "  ⚠>n"
        print(
            f"  {r['scope']:4s} {r['sr_annual']:>+8.3f} {r['sr0_annual']:>+7.3f} "
            f"{r['pwf_oos_sharpe']:>+8.3f} {r['psr_zero']:>7.3f} {r['dsr']:>6.3f} "
            f"{r['pbo']:>6.3f} {trl_s:>8s} {r['n_obs']:>6d}{flag}"
        )

    print(f"\n{'=' * 90}")
    print("PORTFOLIO (PSR/MinTRL are series-only & definitive; DSR shown vs N_trials assumption)")
    print(f"{'=' * 90}")
    p0 = port_rows[0]
    trl = p0["min_trl_95"]
    trl_s = "inf" if not np.isfinite(trl) else f"{trl:,.0f}"
    print(
        f"  PWF Sharpe (annual): {p0['sr_annual']:+.3f}   n_obs={p0['n_obs']}   "
        f"skew={p0['skew']:+.2f}  kurt={p0['kurt']:.2f}"
    )
    print(f"  PSR(0)  = {p0['psr_zero']:.4f}   (P[true Sharpe > 0])")
    print(
        f"  MinTRL@95% = {trl_s} obs   "
        f"({'ALREADY significant — n≥MinTRL' if np.isfinite(trl) and trl <= p0['n_obs'] else 'NOT yet — need more track record'})"
    )
    print(f"  trial Sharpe variance (pooled 27 configs) = {p0['trial_sr_variance']:.4f}")
    print("  DSR vs assumed N_trials:")
    for r in port_rows:
        print(
            f"    N_trials={r['n_trials']:>3d}:  SR0={r['sr0_annual']:+.3f} ann   DSR={r['dsr']:.4f}"
        )

    print(f"\nArtefact → {out}")
    print(
        "\nReading guide: DSR/PSR are P(skill), not Sharpe. >0.95 = strong; ~0.5 = "
        "indistinguishable from luck. PBO is failure rate of IS selection; <0.5 good, "
        ">0.5 means the grid overfits more often than not."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
