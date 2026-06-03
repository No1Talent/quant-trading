"""M3.9: capital-sizing sensitivity (Layer-③ gate ②) on the M3.7 vol-target ensemble.

h4d_capital_sizing.py tested capital scaling on the WFA ensemble with a *static*
per-instrument lot count. The M3.7 vol-target uses a *time-varying* continuous
weight (≈ lots-to-hold), which introduces a new failure mode this script targets:

  **Integer-lot quantization.** Real trading holds whole contracts. At low
  capital the vol-target's continuous weight (0–4×, scaled by capital) rounds to
  a handful of discrete lots, coarsening — and potentially destroying — the fine
  risk sizing that produced the +0.78–1.21 Sharpes. At high capital the rounding
  is negligible and the integer result → the continuous M3.7 result.

Method (post-hoc on the M3.7 weights — no per-tier re-backtest needed because the
1-lot PnL is capital-invariant; only the lot multiplier changes):
  - Run the M3.7 pipeline once per instrument → raw 1-lot OOS PnL + vol-target
    weight wₜ (continuous lots, from causal_vol_target).
  - The TARGET_VOL of 5,000 was calibrated for 1M capital, so target dollar-vol
    scales linearly with capital: scale(C) = C / 1,000,000.
  - desired_lotsₜ = wₜ · scale(C);  actual_lotsₜ = round(desired_lotsₜ)  (≥0 int).
  - PnLₜ(C) = actual_lotsₜ · raw_1lot_pnlₜ  (linear slippage already in raw_1lot).
  - Sharpe / PSR / MinTRL / DD-as-%-of-capital per tier, for AG-solo, AG+CU, AG+I+CU.

NOT modelled here (covered elsewhere, flagged): super-linear market impact —
h4d showed it negligible for this universe ≤10M (Sharpe 0.993→0.972), and M3.8
already stress-tested linear cost (incl. VT's higher turnover) to 5×. This script
isolates the quantization effect that is unique to time-varying sizing.

Pass criterion (gate ②): the minimum capital at which AG-solo / AG+CU Sharpe is
within −0.15 of its continuous (high-capital) value and stays 95%-significant.

Output: research/m39_vt_capital_summary.csv  (gitignored)
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

from research.m35_h4_ensemble_cpcv import INSTRUMENTS, TARGET_VOL  # noqa: E402
from research.m37_ensemble_vol_target import (  # noqa: E402
    VT_MAX_LEVERAGE,
    VT_WINDOW,
    run_one_instrument_vt,
)
from research.overfit_stats import (  # noqa: E402
    min_track_record_length,
    probabilistic_sharpe_ratio,
    sharpe_skew_kurt,
)
from research.vol_target import causal_vol_target  # noqa: E402

# Notional per 1-lot from h4d (DB last-close × multiplier, ~2026-05 prices).
NOTIONAL_PER_LOT = {"AG": 288_510, "I": 80_950, "CU": 523_550}
CAPITAL_TIERS = [300_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]
BASE_CAPITAL = 1_000_000  # TARGET_VOL=5,000 was calibrated at this capital
TRADING_DAYS = 252.0


def per_instrument_weights() -> dict[str, pd.DataFrame]:
    """Run M3.7 PWF once per instrument; return aligned {raw_1lot, weight} frames."""
    out: dict[str, pd.DataFrame] = {}
    for inst in INSTRUMENTS:
        r = run_one_instrument_vt(inst)
        raw = r["raw_oos"]
        if len(raw) == 0:
            continue
        _, weight = causal_vol_target(
            raw, TARGET_VOL, window=VT_WINDOW, max_leverage=VT_MAX_LEVERAGE, return_weights=True
        )
        df = pd.DataFrame({"raw_1lot": raw.reindex(weight.index), "weight": weight}).dropna()
        out[inst["sym"]] = df
        print(f"  {inst['sym']}: {len(df)}d, mean weight={df['weight'].mean():.2f} (cont. lots)")
    return out


def _stats(pnl: pd.Series, capital: float, contracts_avg: float, notional_avg: float) -> dict:
    sr, skew, kurt, n = sharpe_skew_kurt(pnl.values)
    cum = pnl.cumsum()
    max_dd = float((cum - cum.cummax()).min())
    total = float(cum.iloc[-1]) if n else 0.0
    years = n / TRADING_DAYS
    trl = min_track_record_length(sr, skew, kurt, 0.0, 0.95)
    return {
        "sharpe": sr * np.sqrt(TRADING_DAYS) if n else float("nan"),
        "psr0": probabilistic_sharpe_ratio(sr, n, skew, kurt, 0.0),
        "min_trl": trl,
        "significant": bool(np.isfinite(trl) and trl <= n),
        "ann_pct": (total / years) / capital * 100 if years else float("nan"),
        "dd_pct_cap": abs(max_dd) / capital * 100,
        "avg_lots": contracts_avg,
        "deployed_pct": notional_avg / capital * 100,
        "n": n,
    }


def run_tier(capital: float, per_inst: dict[str, pd.DataFrame]) -> dict:
    scale = capital / BASE_CAPITAL
    pnls, avg_lots, avg_notional = {}, {}, {}
    for sym, df in per_inst.items():
        lots = (df["weight"] * scale).round().clip(lower=0)
        pnls[sym] = lots * df["raw_1lot"]
        avg_lots[sym] = float(lots.mean())
        avg_notional[sym] = float(lots.mean()) * NOTIONAL_PER_LOT[sym]

    panel = pd.DataFrame(pnls).dropna(how="any")
    if len(panel) == 0:
        return {"capital": capital, "error": "no_intersection"}

    def combo(cols: list[str]) -> dict:
        cols = [c for c in cols if c in panel.columns]
        series = panel[cols].sum(axis=1)
        return _stats(
            series, capital, sum(avg_lots[c] for c in cols), sum(avg_notional[c] for c in cols)
        )

    return {
        "capital": capital,
        "scale": scale,
        "ag_lots": avg_lots.get("AG", float("nan")),
        "cu_lots": avg_lots.get("CU", float("nan")),
        "i_lots": avg_lots.get("I", float("nan")),
        "ag_solo": combo(["AG"]),
        "ag_cu": combo(["AG", "CU"]),
        "full": combo(["AG", "I", "CU"]),
    }


def _fmt(d: dict) -> str:
    """One compact cell: Sharpe (★ if 95%-significant) / DD%cap / deployed%."""
    star = "★" if d["significant"] else " "
    return f"{d['sharpe']:+.2f}{star} dd{d['dd_pct_cap']:>4.1f} dep{d['deployed_pct']:>3.0f}"


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("vnpy_ctastrategy.backtesting").setLevel(logging.ERROR)
    logging.getLogger("cpcv").setLevel(logging.WARNING)

    print(f"\n{'=' * 92}")
    print("M3.9 capital-sizing (integer-lot quantization) on M3.7 VT weights")
    print(
        f"vol-target {VT_WINDOW}d/{VT_MAX_LEVERAGE}×; target $vol scales as C/{BASE_CAPITAL:,.0f}"
    )
    print(f"{'=' * 92}")

    print("\nRunning M3.7 PWF once per instrument...")
    per_inst = per_instrument_weights()
    if "AG" not in per_inst:
        print("  ERROR: AG produced no series.")
        return 1

    rows = [r for c in CAPITAL_TIERS if "error" not in (r := run_tier(c, per_inst))]

    print(f"\n{'=' * 92}")
    print("CAPITAL TIERS — Sharpe (★=95%-significant) / DD%cap / deployed%")
    print(f"{'=' * 92}")
    print(f"  {'capital':>11} {'AGlots':>7} | {'AG-solo':>16} | {'AG+CU':>16} | {'AG+I+CU':>16}")
    out_rows = []
    for r in rows:
        print(
            f"  {r['capital']:>11,.0f} {r['ag_lots']:>7.1f} | {_fmt(r['ag_solo'])} | "
            f"{_fmt(r['ag_cu'])} | {_fmt(r['full'])}"
        )
        out_rows.append(
            {
                "capital": r["capital"],
                "ag_lots": r["ag_lots"],
                "cu_lots": r["cu_lots"],
                "ag_solo_sharpe": r["ag_solo"]["sharpe"],
                "ag_solo_sig": r["ag_solo"]["significant"],
                "ag_solo_dd_pct": r["ag_solo"]["dd_pct_cap"],
                "ag_solo_ann_pct": r["ag_solo"]["ann_pct"],
                "ag_cu_sharpe": r["ag_cu"]["sharpe"],
                "ag_cu_sig": r["ag_cu"]["significant"],
                "ag_cu_dd_pct": r["ag_cu"]["dd_pct_cap"],
                "ag_cu_ann_pct": r["ag_cu"]["ann_pct"],
                "full_sharpe": r["full"]["sharpe"],
            }
        )
    pd.DataFrame(out_rows).to_csv(
        REPO_ROOT / "research" / "m39_vt_capital_summary.csv", index=False
    )

    # Verdict: min capital where AG-solo & AG+CU are within -0.15 of their 10M (≈continuous) value.
    top = rows[-1]
    print(
        f"\n{'=' * 92}\nVERDICT (Layer-③ gate ②: capital scalability + lot quantization)\n{'=' * 92}"
    )
    for label, key in [("AG-solo", "ag_solo"), ("AG+CU", "ag_cu")]:
        ref = top[key]["sharpe"]
        viable = [
            r["capital"] for r in rows if r[key]["sharpe"] >= ref - 0.15 and r[key]["significant"]
        ]
        min_cap = min(viable) if viable else None
        cap_s = f"{min_cap:,.0f}" if min_cap else "none in range"
        print(
            f"  {label:8s}: high-capital Sharpe {ref:+.3f}; "
            f"min viable capital (within -0.15 & significant) = {cap_s}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
