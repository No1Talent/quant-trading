"""H4b sensitivity sweeps for the H4 ensemble.

Inputs the per-instrument scaled-OOS daily PnL panel produced by
research/h4_ensemble.py (h4_ensemble_daily_panel_union.csv) and recomputes
portfolio stats under five framings. The WFA itself is not re-run — all
variants are deterministic functions of the saved panel.

Variants:
  V0  intersection of all 3                        (baseline, reproduces v6)
  V1  union + zero-fill across all 3               (no diversification cover)
  V2  intersection of {I, CU}      (drop AG)       (LOO)
  V3  intersection of {AG, CU}     (drop I)        (LOO)
  V4  intersection of {AG, I}      (drop CU)       (LOO)

Verdict criteria:
  - V1 Sharpe within ~0.10 of V0  → result not pure diversification artefact;
    early CU-solo era still ensemble-friendly when others are zero-filled.
  - min(V2, V3, V4) Sharpe still > best-single intersection Sharpe (+0.723)
    → no single instrument is carrying the +0.993 result.

If a LOO variant beats the 3-way portfolio, that instrument is a NET DRAG
on the ensemble (a related but distinct finding worth noting).
"""

from __future__ import annotations

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

PANEL_CSV = REPO_ROOT / "research" / "h4_ensemble_daily_panel_union.csv"


def summarize(s: pd.Series, label: str) -> dict:
    """Daily Sharpe (×√252), total PnL, max DD on cumulative PnL, daily std."""
    if len(s) == 0:
        return {
            "variant": label,
            "days": 0,
            "sharpe_ann": float("nan"),
            "daily_mean": float("nan"),
            "daily_std": float("nan"),
            "total_pnl": 0.0,
            "max_dd": 0.0,
        }
    mean = float(s.mean())
    std = float(s.std())
    sharpe = (mean / std) * np.sqrt(252) if std > 0 else float("nan")
    cum = s.cumsum()
    peak = cum.cummax()
    max_dd = float((cum - peak).min())
    return {
        "variant": label,
        "days": int(len(s)),
        "sharpe_ann": sharpe,
        "daily_mean": mean,
        "daily_std": std,
        "total_pnl": float(cum.iloc[-1]),
        "max_dd": max_dd,
    }


def variant_intersection(panel: pd.DataFrame, cols: list[str], label: str) -> dict:
    """Drop-NA across `cols` then sum — apples-to-apples intersection Sharpe."""
    sub = panel[cols].dropna(how="any")
    st = summarize(sub.sum(axis=1), label)
    st["columns"] = ",".join(cols)
    st["date_min"] = str(sub.index.min().date()) if len(sub) else "-"
    st["date_max"] = str(sub.index.max().date()) if len(sub) else "-"
    return st


def variant_union_zero(panel: pd.DataFrame, cols: list[str], label: str) -> dict:
    """Fill NaN with 0 — instrument contributes 0 PnL on dates it isn't running."""
    sub = panel[cols].fillna(0.0)
    # Trim leading rows where ALL cols are still 0 (before any fold started)
    nonzero_mask = (sub != 0).any(axis=1)
    if nonzero_mask.any():
        first_real = nonzero_mask.idxmax()
        sub = sub.loc[first_real:]
    st = summarize(sub.sum(axis=1), label)
    st["columns"] = ",".join(cols)
    st["date_min"] = str(sub.index.min().date()) if len(sub) else "-"
    st["date_max"] = str(sub.index.max().date()) if len(sub) else "-"
    return st


def main() -> int:
    if not PANEL_CSV.exists():
        print(f"ERROR: {PANEL_CSV} not found. Run research/h4_ensemble.py first.")
        return 1

    panel = pd.read_csv(PANEL_CSV, index_col=0, parse_dates=True)
    print(f"\n{'#' * 90}\n# H4b sensitivity sweeps (from saved union panel)\n{'#' * 90}")
    print(f"Panel: {PANEL_CSV.name}  |  rows={len(panel)}  |  cols={list(panel.columns)}")
    print(f"Date range: {panel.index.min().date()} → {panel.index.max().date()}")
    print("Per-column coverage (non-NaN days):")
    for c in panel.columns:
        n = int(panel[c].notna().sum())
        d0 = panel[c].first_valid_index()
        d1 = panel[c].last_valid_index()
        print(
            f"  {c:3s}: {n:5d} days  ({d0.date() if d0 is not None else '-'} → {d1.date() if d1 is not None else '-'})"
        )

    cols = list(panel.columns)
    if set(cols) != {"AG", "I", "CU"}:
        print(f"WARNING: expected columns AG/I/CU, got {cols} — proceeding regardless")

    variants = []

    # V0 baseline (intersection of all 3)
    variants.append(variant_intersection(panel, ["AG", "I", "CU"], "V0_intersection_3"))

    # V1 union + zero-fill (3 instruments)
    variants.append(variant_union_zero(panel, ["AG", "I", "CU"], "V1_union_zerofill_3"))

    # V2-V4 leave-one-out (intersection of remaining pair)
    variants.append(variant_intersection(panel, ["I", "CU"], "V2_LOO_drop_AG"))
    variants.append(variant_intersection(panel, ["AG", "CU"], "V3_LOO_drop_I"))
    variants.append(variant_intersection(panel, ["AG", "I"], "V4_LOO_drop_CU"))

    # Per-instrument intersection-period Sharpes for reference
    print(f"\n{'=' * 90}\nPer-instrument (intersection of 3) reference Sharpes\n{'=' * 90}")
    inter3 = panel[["AG", "I", "CU"]].dropna(how="any")
    inst_stats = []
    for c in inter3.columns:
        st = summarize(inter3[c], c)
        inst_stats.append(st)
        print(
            f"  {c:3s}: Sharpe={st['sharpe_ann']:+.3f}  total={st['total_pnl']:>+12,.0f}  maxDD={st['max_dd']:>+12,.0f}"
        )
    best_single_sharpe = max(s["sharpe_ann"] for s in inst_stats)

    # Print variants table
    print(f"\n{'=' * 90}\nPortfolio variants\n{'=' * 90}")
    header = f"{'variant':22s} {'cols':10s} {'days':>5s} {'Sharpe':>8s} {'total':>14s} {'maxDD':>14s} {'σ_daily':>10s} {'range':>27s}"
    print(header)
    print("-" * len(header))
    for v in variants:
        print(
            f"{v['variant']:22s} {v['columns']:10s} {v['days']:>5d} "
            f"{v['sharpe_ann']:>+8.3f} {v['total_pnl']:>+14,.0f} "
            f"{v['max_dd']:>+14,.0f} {v['daily_std']:>10.1f} "
            f"{v['date_min']} → {v['date_max']}"
        )

    # Verdict
    v0 = next(v for v in variants if v["variant"] == "V0_intersection_3")
    v1 = next(v for v in variants if v["variant"] == "V1_union_zerofill_3")
    loo_variants = [v for v in variants if v["variant"].startswith(("V2", "V3", "V4"))]

    print(f"\n{'=' * 90}\nVERDICT\n{'=' * 90}")
    print(f"  V0 baseline Sharpe (intersection-3):       {v0['sharpe_ann']:+.3f}")
    print(f"  V1 union+zerofill Sharpe:                  {v1['sharpe_ann']:+.3f}")
    print(
        f"  Δ V1 - V0:                                 {v1['sharpe_ann'] - v0['sharpe_ann']:+.3f}"
    )
    if abs(v1["sharpe_ann"] - v0["sharpe_ann"]) <= 0.10:
        print("    → [DIVERSIFICATION_ROBUST] V1 ~ V0; early CU-solo era doesn't tank the result.")
    elif v1["sharpe_ann"] < v0["sharpe_ann"]:
        print(
            "    → [DIVERSIFICATION_FRAGILE] V1 < V0 by >0.10; full-cover periods are doing real lifting."
        )
    else:
        print("    → [V1_BETTER] union > intersection — early CU era is helping, not hurting.")

    print("\n  LOO variants (intersection of remaining pair):")
    for v in loo_variants:
        delta = v["sharpe_ann"] - v0["sharpe_ann"]
        delta_vs_best = v["sharpe_ann"] - best_single_sharpe
        flag = ""
        if v["sharpe_ann"] > v0["sharpe_ann"]:
            flag = "  [DROPPED_DRAG]"  # the removed instrument was a drag
        elif v["sharpe_ann"] < best_single_sharpe:
            flag = "  [DROPPED_CARRIES]"  # the removed instrument was carrying
        print(
            f"    {v['variant']:22s} Sharpe={v['sharpe_ann']:+.3f}  "
            f"ΔvsV0={delta:+.3f}  ΔvsBestSingle={delta_vs_best:+.3f}{flag}"
        )

    min_loo = min(v["sharpe_ann"] for v in loo_variants)
    if min_loo > best_single_sharpe:
        print(f"\n  min(LOO Sharpe)={min_loo:+.3f} > best single ({best_single_sharpe:+.3f})")
        print("    → [NO_SINGLE_CARRIER] no single instrument is carrying the +0.993 result.")
    else:
        carrier = min(loo_variants, key=lambda v: v["sharpe_ann"])
        dropped = carrier["variant"].split("_")[-1]
        print(f"\n  min(LOO Sharpe)={min_loo:+.3f} ≤ best single ({best_single_sharpe:+.3f})")
        print(
            f"    → [SINGLE_CARRIER_RISK] dropping {dropped} pulls portfolio below the best single instrument."
        )

    # Persist
    out = pd.DataFrame(variants)
    out_path = REPO_ROOT / "research" / "h4b_sensitivity_summary.csv"
    out.to_csv(out_path, index=False)
    print(f"\nArtefact: {out_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
