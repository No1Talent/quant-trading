"""H3 sensitivity: how much of DoubleMa/AG daily's +0.344 Sharpe is fold-15 alone?

Fold 15 (2023-11 to 2024-07, mid silver bull run) contributed OOS Sharpe +1.73
and OOS return +3.10% — by far the strongest single fold. Recompute aggregates
after dropping it; if signal collapses, alpha is one-regime-dependent.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402


def main() -> int:
    csv = REPO_ROOT / "research" / "wfa_results_daily_continuous.csv"
    df = pd.read_csv(csv)
    dm_ag = df[df["strategy"] == "DoubleMa/AG"].reset_index(drop=True)

    print(f"{'=' * 70}\nH3: DoubleMa/AG daily fold-15 sensitivity\n{'=' * 70}\n")

    def stats(sub: pd.DataFrame, label: str) -> None:
        oos = sub["oos_sharpe"].dropna()
        corr = sub[["is_sharpe", "oos_sharpe"]].corr().iloc[0, 1]
        ret = sub["oos_return_pct"]
        print(f"  {label}:")
        print(f"    folds:                  {len(sub)}")
        print(f"    OOS Sharpe mean:        {oos.mean():+.3f}")
        print(f"    OOS Sharpe median:      {oos.median():+.3f}")
        print(
            f"    OOS positive folds:     {(oos > 0).sum()}/{len(oos)}  ({(oos > 0).mean()*100:.0f}%)"
        )
        print(f"    Total OOS return %:     {ret.sum():+.3f}")
        print(f"    IS-OOS corr:            {corr:+.3f}")
        print(f"    Best single fold OOS:   {oos.max():+.3f}")
        print()

    stats(dm_ag, "All 17 folds (original)")

    # Drop the fold with largest OOS return (fold 15 per the data)
    max_idx = dm_ag["oos_return_pct"].idxmax()
    dropped_fold = dm_ag.loc[max_idx]
    print(
        f"  Dropping fold {int(dropped_fold['fold'])}: test {dropped_fold['test_end']}, "
        f"OOS Sharpe {dropped_fold['oos_sharpe']:+.3f}, OOS return {dropped_fold['oos_return_pct']:+.3f}%\n"
    )

    dm_ag_minus = dm_ag.drop(max_idx).reset_index(drop=True)
    stats(dm_ag_minus, "After dropping the biggest-return fold")

    delta_sharpe = dm_ag_minus["oos_sharpe"].dropna().mean() - dm_ag["oos_sharpe"].dropna().mean()
    delta_return = dm_ag_minus["oos_return_pct"].sum() - dm_ag["oos_return_pct"].sum()
    print(f"{'=' * 70}\nVERDICT\n{'=' * 70}")
    print(f"  Δ OOS Sharpe mean:    {delta_sharpe:+.3f}")
    print(f"  Δ Total OOS return:   {delta_return:+.3f}%")
    if (
        dm_ag_minus["oos_sharpe"].dropna().mean() > 0.15
        and dm_ag_minus["oos_return_pct"].sum() > 2.0
    ):
        print(
            "  [ROBUST] Signal holds without fold 15. 2020-2023 base + other folds carry the alpha."
        )
    elif dm_ag_minus["oos_sharpe"].dropna().mean() > 0:
        print(
            "  [SOFT] Signal degraded but still positive. Fold 15 contributed meaningfully but isn't sole driver."
        )
    else:
        print(
            "  [DEPENDENT] Signal evaporates without fold 15. Alpha is one-regime-dependent (2024 silver bull)."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
