"""M3.7: H4 ensemble under PWF with a *causal* daily vol-target (fixes M3.6b).

M3.6b diagnosed the +0.526 PWF portfolio Sharpe (M3.5) as fat-tailed (kurtosis
63) because the per-split inverse-*train*-vol weight over-leverages an instrument
when its test-window vol diverges from the train window (AG silver, 2026: ~16×).

This script keeps everything in M3.5 identical — same instruments, PWF folds,
grid, purge — EXCEPT the sizing: instead of one weight per split from the train
window, each instrument's concatenated raw OOS PnL is re-sized daily by a causal
trailing-vol target (research.vol_target.causal_vol_target). It then re-runs the
M3.6 deflation (PSR / MinTRL / DSR) on the vol-targeted series to see whether the
"+0.526 not-yet-significant" verdict flips once the tails are removed.

Apples-to-apples chain: h4_ensemble (WFA) → m35 (PWF) → m37 (PWF + causal VT).

Outputs (research/, gitignored like the other stats CSVs):
  - m37_vt_panel.csv     per-instrument vol-targeted OOS daily PnL (intersection)
  - m37_vt_stats.csv     per-instrument + portfolio summary with deflation
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# Reuse M3.5's exact config + helpers so the only change is the sizing method.
from research.m35_h4_ensemble_cpcv import (  # noqa: E402
    DM_GRID,
    END_DATE,
    INSTRUMENTS,
    MIN_TRADES,
    PWF_N_FOLDS,
    PWF_PURGE_DAYS,
    TARGET_VOL,
    _daily_pnl_series,
    summarize_series,
    yearly_sub_sharpes,
)
from research.overfit_stats import (  # noqa: E402
    deflated_sharpe_ratio,
    min_track_record_length,
    probabilistic_sharpe_ratio,
    sharpe_skew_kurt,
)
from research.vol_target import causal_vol_target  # noqa: E402

# Vol-target params — midpoint of the M3.6b robust band (window 40-126 × cap 3-5).
VT_WINDOW = 63
VT_MAX_LEVERAGE = 4.0
TRADING_DAYS = 252.0
M36_CSV = REPO_ROOT / "research" / "m36_overfit_stats.csv"

# M3.5 PWF (pre-vol-target) reference Sharpes, for the comparison table.
M35_PWF_REF = {"AG": 0.630, "I": -0.266, "CU": 0.140, "PORTFOLIO": 0.526}


def run_one_instrument_vt(inst: dict) -> dict:
    """PWF with curve capture, then concat RAW OOS PnL and apply the causal
    daily vol-target (replacing M3.5's per-split inverse-train-vol weight)."""
    from research.cpcv import run_pwf
    from strategies.double_ma_strategy import DoubleMaStrategy

    _, curves = run_pwf(
        strategy_class=DoubleMaStrategy,
        param_grid=DM_GRID,
        fixed_params={"fixed_size": inst.get("fixed_size", 1)},
        vt_symbol=inst["vt_symbol"],
        interval="1d",
        start=inst["start"],
        end=END_DATE,
        n_folds=PWF_N_FOLDS,
        purge_days=PWF_PURGE_DAYS,
        metric="sharpe_ratio",
        min_trades=MIN_TRADES,
        return_curves=True,
        **inst["bt"],
    )

    raw_pieces = []
    for c in curves:
        oos = _daily_pnl_series(c["oos_daily_df"])
        if len(oos) > 0:
            raw_pieces.append(oos)
    if not raw_pieces:
        empty = pd.Series(dtype=float)
        return {"sym": inst["sym"], "raw_oos": empty, "vt_oos": empty}

    raw = pd.concat(raw_pieces).sort_index()
    raw = raw.groupby(raw.index).sum()  # coalesce any boundary-day overlap
    vt = causal_vol_target(raw, TARGET_VOL, window=VT_WINDOW, max_leverage=VT_MAX_LEVERAGE)
    return {"sym": inst["sym"], "raw_oos": raw, "vt_oos": vt}


def _load_trial_variances() -> dict:
    """Read per-instrument + pooled trial-Sharpe variance from the M3.6 artifact.
    Trial variance is a property of the 9-config grid (sizing-independent), so it
    is legitimate to reuse for the VT-series DSR. Returns {} if the CSV is absent
    (DSR is then skipped — PSR/MinTRL are the definitive verdict-flippers anyway)."""
    if not M36_CSV.exists():
        return {}
    m36 = pd.read_csv(M36_CSV)
    out: dict = {}
    for _, r in m36.iterrows():
        out[r["scope"]] = {"var": float(r["trial_sr_variance"]), "n_trials": int(r["n_trials"])}
    return out


def _deflate(series: pd.Series, trial: dict | None) -> dict:
    sr, skew, kurt, n = sharpe_skew_kurt(series.values)
    row = {
        "sharpe_ann": sr * np.sqrt(TRADING_DAYS),
        "sr_per_period": sr,
        "n_obs": n,
        "skew": skew,
        "kurt": kurt,
        "psr_zero": probabilistic_sharpe_ratio(sr, n, skew, kurt, 0.0),
        "min_trl_95": min_track_record_length(sr, skew, kurt, 0.0, 0.95),
    }
    if trial:
        dsr = deflated_sharpe_ratio(sr, n, skew, kurt, trial["n_trials"], trial["var"])
        row["dsr"] = dsr.dsr
        row["dsr_n_trials"] = trial["n_trials"]
    else:
        row["dsr"] = float("nan")
        row["dsr_n_trials"] = 0
    return row


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("cpcv").setLevel(logging.WARNING)
    logging.getLogger("wfa").setLevel(logging.WARNING)

    print(f"\n{'#' * 92}")
    print("# M3.7 — H4 ensemble under PWF + CAUSAL DAILY VOL-TARGET")
    print(
        f"# vol-target: {VT_WINDOW}d trailing σ, cap {VT_MAX_LEVERAGE}×, target {TARGET_VOL:,.0f}"
    )
    print(f"{'#' * 92}")

    results = [run_one_instrument_vt(inst) for inst in INSTRUMENTS]
    for r in results:
        print(f"  {r['sym']:3s}: raw OOS {len(r['raw_oos'])}d → vol-targeted {len(r['vt_oos'])}d")

    panel = pd.DataFrame({r["sym"]: r["vt_oos"] for r in results})
    intersection = panel.dropna(how="any")
    if len(intersection) == 0:
        print("  ERROR: empty intersection panel.")
        return 1
    portfolio = intersection.sum(axis=1)

    trial_vars = _load_trial_variances()
    if not trial_vars:
        print(f"\n  (note: {M36_CSV.name} not found — DSR skipped; run m36 first for DSR)")

    # --- Per-instrument + portfolio summary with deflation ---
    print(f"\n{'=' * 92}")
    print("PER-INSTRUMENT & PORTFOLIO (vol-targeted), with M3.5 comparison + deflation")
    print(f"{'=' * 92}")
    print(
        f"  {'scope':10s} {'M35 PWF':>8s} {'VT Sharpe':>9s} {'kurt':>6s} "
        f"{'PSR(0)':>7s} {'DSR':>6s} {'MinTRL':>9s} {'n':>5s}"
    )
    rows = []
    for col in list(intersection.columns) + ["PORTFOLIO"]:
        series = portfolio if col == "PORTFOLIO" else intersection[col]
        defl = _deflate(series, trial_vars.get(col))
        base = summarize_series(series, col)
        defl.update(
            {"scope": col, "total_pnl": base.get("total_pnl"), "max_dd": base.get("max_dd")}
        )
        rows.append(defl)
        trl = defl["min_trl_95"]
        trl_s = "inf" if not np.isfinite(trl) else f"{trl:,.0f}"
        flag = "" if np.isfinite(trl) and trl <= defl["n_obs"] else " ⚠"
        dsr_s = "  —  " if not np.isfinite(defl["dsr"]) else f"{defl['dsr']:.3f}"
        print(
            f"  {col:10s} {M35_PWF_REF.get(col, float('nan')):>+8.3f} "
            f"{defl['sharpe_ann']:>+9.3f} {defl['kurt']:>6.1f} {defl['psr_zero']:>7.3f} "
            f"{dsr_s:>6s} {trl_s:>9s}{flag} {defl['n_obs']:>5d}"
        )

    # --- Verdict on the portfolio ---
    port = next(r for r in rows if r["scope"] == "PORTFOLIO")
    print(f"\n{'=' * 92}\nVERDICT (portfolio)\n{'=' * 92}")
    print("  M3.5 PWF: Sharpe +0.526, kurt 63, PSR(0)=0.943, MinTRL 2643 > 2441  → NOT significant")
    print(
        f"  M3.7 VT : Sharpe {port['sharpe_ann']:+.3f}, kurt {port['kurt']:.1f}, "
        f"PSR(0)={port['psr_zero']:.3f}, MinTRL "
        f"{port['min_trl_95']:,.0f} vs {port['n_obs']}"
    )
    sig = np.isfinite(port["min_trl_95"]) and port["min_trl_95"] <= port["n_obs"]
    print(
        f"  → {'[FLIPS] portfolio Sharpe is now 95%-significant for >0' if sig else '[STILL SHORT] not yet 95%-significant'}"
        f"; PSR(0)={port['psr_zero']:.3f}"
    )

    yearly = yearly_sub_sharpes(portfolio)
    if len(yearly):
        complete = yearly[~yearly["incomplete"]]["sharpe"]
        print(
            f"\n  Yearly sub-Sharpes (complete yrs): mean={complete.mean():+.3f} "
            f"median={complete.median():+.3f} std={complete.std():.3f} "
            f"pos%={(complete > 0).mean() * 100:.0f}"
        )

    out_dir = REPO_ROOT / "research"
    intersection.to_csv(out_dir / "m37_vt_panel.csv")
    pd.DataFrame(rows).to_csv(out_dir / "m37_vt_stats.csv", index=False)
    print(f"\nArtefacts → {out_dir / 'm37_vt_panel.csv'}, {out_dir / 'm37_vt_stats.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
