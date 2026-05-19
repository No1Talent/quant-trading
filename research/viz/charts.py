"""Chart factories: DataFrame -> matplotlib.Figure.

Pure functions — no file I/O, no rcParams mutation, no `plt.show()`. The
renderer (or a Streamlit page) owns the lifecycle: it calls a factory,
saves or displays the Figure, then closes it.

Conventions:
  - Every factory takes a DataFrame plus optional `title` / kwargs and
    returns a `matplotlib.figure.Figure`.
  - Factories skip themselves and return None when the input lacks the
    required columns (so the registry can call them defensively).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from research.viz.themes import PALETTE

# --------------------------------------------------------------------------- #
# Family A — per-fold WFA results
# --------------------------------------------------------------------------- #


def fig_fold_sharpe_bars(df: pd.DataFrame, title: str = "") -> Figure | None:
    """IS vs OOS Sharpe per fold. Grouped bars.

    If the frame contains multiple groups (`df.attrs["group_col"]`), each
    group gets its own subplot row.
    """
    if not {"fold", "is_sharpe", "oos_sharpe"}.issubset(df.columns):
        return None

    group_col = df.attrs.get("group_col")
    groups = sorted(df[group_col].dropna().unique()) if group_col else [None]
    n = len(groups)

    fig, axes = plt.subplots(n, 1, figsize=(11, 3.0 * n), squeeze=False)
    for ax, grp in zip(axes[:, 0], groups, strict=True):
        sub = df if grp is None else df[df[group_col] == grp]
        x = np.arange(len(sub))
        width = 0.4
        ax.bar(x - width / 2, sub["is_sharpe"], width, color=PALETTE["is"], label="IS")
        ax.bar(x + width / 2, sub["oos_sharpe"], width, color=PALETTE["oos"], label="OOS")
        ax.axhline(0, color="#595959", linewidth=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(sub["fold"].astype(int), fontsize=8)
        ax.set_ylabel("Sharpe (ann.)")
        sub_title = f"{title} — {grp}" if grp else title
        ax.set_title(sub_title or "IS vs OOS Sharpe per fold")
        ax.legend(loc="best")
    axes[-1, 0].set_xlabel("Fold")
    fig.tight_layout()
    return fig


def fig_is_vs_oos_scatter(df: pd.DataFrame, title: str = "") -> Figure | None:
    """IS Sharpe vs OOS Sharpe scatter. Each point coloured by OOS sign.

    Diagonal y=x reference shows perfect persistence; horizontal y=0 shows
    OOS profitability threshold. Pearson correlation in the corner.
    """
    if not {"is_sharpe", "oos_sharpe"}.issubset(df.columns):
        return None
    clean = df.dropna(subset=["is_sharpe", "oos_sharpe"])
    if clean.empty:
        return None

    fig, ax = plt.subplots(figsize=(7, 6))
    colors = np.where(clean["oos_sharpe"] >= 0, PALETTE["oos_pos"], PALETTE["oos_neg"])
    ax.scatter(
        clean["is_sharpe"],
        clean["oos_sharpe"],
        c=colors,
        s=55,
        alpha=0.75,
        edgecolor="white",
        linewidth=0.6,
    )

    lo = float(min(clean["is_sharpe"].min(), clean["oos_sharpe"].min(), 0))
    hi = float(max(clean["is_sharpe"].max(), clean["oos_sharpe"].max(), 0))
    pad = 0.1 * (hi - lo + 1)
    ax.plot(
        [lo - pad, hi + pad],
        [lo - pad, hi + pad],
        color=PALETTE["diagonal"],
        linewidth=0.8,
        linestyle="--",
        label="y = x",
    )
    ax.axhline(0, color="#595959", linewidth=0.6)
    ax.axvline(0, color="#595959", linewidth=0.6)

    corr = clean["is_sharpe"].corr(clean["oos_sharpe"])
    n_pos = int((clean["oos_sharpe"] > 0).sum())
    ax.text(
        0.02,
        0.98,
        f"folds = {len(clean)}\nOOS+ = {n_pos}\ncorr = {corr:+.2f}",
        transform=ax.transAxes,
        va="top",
        ha="left",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor="#BFBFBF"),
    )

    ax.set_xlabel("IS Sharpe")
    ax.set_ylabel("OOS Sharpe")
    ax.set_title(title or "IS → OOS persistence")
    ax.legend(loc="lower right")
    fig.tight_layout()
    return fig


def fig_oos_metrics_panel(df: pd.DataFrame, title: str = "") -> Figure | None:
    """2x2 panel: OOS Sharpe, Return%, MaxDD%, Trades — all per fold."""
    cols = ["oos_sharpe", "oos_return_pct", "oos_max_dd_pct", "oos_trades"]
    if not ({"fold"} | set(cols)).issubset(df.columns):
        return None

    fig, axes = plt.subplots(2, 2, figsize=(12, 7))
    # PALETTE values can be str (single colour) or list[str] (instrument cycle);
    # matplotlib accepts either, so the 4th tuple element is widened to Any.
    plots: list[tuple[Any, str, str, Any]] = [
        (axes[0, 0], "oos_sharpe", "OOS Sharpe", PALETTE["oos"]),
        (axes[0, 1], "oos_return_pct", "OOS Return %", PALETTE["is"]),
        (axes[1, 0], "oos_max_dd_pct", "OOS Max DD %", PALETTE["drawdown"]),
        (axes[1, 1], "oos_trades", "OOS Trades", "#9270CA"),
    ]
    group_col = df.attrs.get("group_col")
    for ax, col, label, color in plots:
        if group_col and df[group_col].nunique() > 1:
            for grp, sub in df.groupby(group_col):
                ax.plot(sub["fold"], sub[col], marker="o", label=str(grp), linewidth=1.2)
            ax.legend(fontsize=7, ncol=2)
        else:
            ax.bar(df["fold"], df[col], color=color, alpha=0.85)
        ax.axhline(0, color="#595959", linewidth=0.5)
        ax.set_title(label)
        ax.set_xlabel("Fold")
    fig.suptitle(title or "OOS metrics per fold", fontsize=13, fontweight="bold")
    fig.tight_layout()
    return fig


def fig_param_winners(df: pd.DataFrame, title: str = "") -> Figure | None:
    """Distribution of winning hyperparameter values across folds.

    One subplot per parameter key found in `best_params_parsed`. A flat
    distribution suggests no stable optimum (likely overfit / noise);
    a concentrated peak suggests a robust regime.
    """
    if "best_params_parsed" not in df.columns:
        return None
    parsed = df["best_params_parsed"].dropna().tolist()
    if not parsed:
        return None

    keys = sorted({k for p in parsed if isinstance(p, dict) for k in p.keys()})
    if not keys:
        return None

    fig, axes = plt.subplots(1, len(keys), figsize=(4 * len(keys), 3.5), squeeze=False)
    for ax, key in zip(axes[0], keys, strict=True):
        vals = [p[key] for p in parsed if isinstance(p, dict) and key in p]
        counts = Counter(vals)
        xs = sorted(counts.keys(), key=lambda v: (isinstance(v, str), v))
        ys = [counts[x] for x in xs]
        ax.bar(range(len(xs)), ys, color=PALETTE["is"], alpha=0.85)
        ax.set_xticks(range(len(xs)))
        ax.set_xticklabels([str(x) for x in xs], rotation=0)
        ax.set_title(key)
        ax.set_ylabel("Fold count")
    fig.suptitle(title or "Winning-param distribution across folds", fontsize=12, fontweight="bold")
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------- #
# Family C — daily PnL panels
# --------------------------------------------------------------------------- #


def fig_cumulative_pnl(df: pd.DataFrame, title: str = "") -> Figure | None:
    """Per-instrument cumulative net PnL + equal-weight portfolio overlay.

    Treats NaN as 0 inside cumsum (so missing-data days don't break the
    portfolio line) but the per-instrument line is drawn from the
    instrument's own data only.
    """
    if df.empty:
        return None
    instruments = [c for c in df.columns if c.lower() != "date"]
    if not instruments:
        return None

    fig, ax = plt.subplots(figsize=(12, 6))
    palette = PALETTE["instruments"]
    for i, col in enumerate(instruments):
        series = df[col].fillna(0).cumsum()
        ax.plot(
            series.index, series.values, label=col, color=palette[i % len(palette)], linewidth=1.4
        )

    portfolio = df[instruments].fillna(0).sum(axis=1).cumsum()
    ax.plot(
        portfolio.index,
        portfolio.values,
        label="PORTFOLIO",
        color=PALETTE["portfolio"],
        linewidth=2.0,
    )

    ax.set_title(title or "Cumulative net PnL")
    ax.set_ylabel("Cumulative PnL (CNY)")
    ax.legend(loc="best", ncol=min(4, len(instruments) + 1))
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def fig_drawdown(df: pd.DataFrame, title: str = "") -> Figure | None:
    """Portfolio drawdown curve from equal-weight daily PnL."""
    if df.empty:
        return None
    instruments = [c for c in df.columns if c.lower() != "date"]
    if not instruments:
        return None

    equity = df[instruments].fillna(0).sum(axis=1).cumsum()
    peak = equity.cummax()
    dd = equity - peak

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.fill_between(dd.index, dd.values, 0, color=PALETTE["drawdown"], alpha=0.55)
    ax.plot(dd.index, dd.values, color=PALETTE["drawdown"], linewidth=1.0)
    ax.set_title(title or "Portfolio drawdown")
    ax.set_ylabel("Drawdown (CNY)")
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------- #
# Family D — summary tables
# --------------------------------------------------------------------------- #


def fig_summary_table(df: pd.DataFrame, title: str = "", max_cols: int = 12) -> Figure | None:
    """Render a summary DataFrame as a styled table image.

    Numeric cells are formatted to 3 decimal places; columns past
    `max_cols` are dropped to keep the figure legible.
    """
    if df.empty:
        return None
    show = df.copy()
    if len(show.columns) > max_cols:
        show = show.iloc[:, :max_cols]

    def _fmt(v: Any) -> str:
        if isinstance(v, int | np.integer):
            return f"{v:d}"
        if isinstance(v, float | np.floating):
            return "—" if pd.isna(v) else f"{v:.3f}"
        if pd.isna(v):
            return "—"
        return str(v)

    cells = [[_fmt(v) for v in row] for row in show.itertuples(index=False)]

    height = max(2.2, 0.35 * (len(show) + 2))
    width = max(7, 1.1 * len(show.columns))
    fig, ax = plt.subplots(figsize=(width, height))
    ax.axis("off")
    table = ax.table(cellText=cells, colLabels=list(show.columns), loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    table.scale(1.0, 1.3)

    for (row, _col), cell in table.get_celld().items():
        if row == 0:
            cell.set_text_props(weight="bold", color="white")
            cell.set_facecolor("#262626")
        else:
            cell.set_facecolor("#FAFAFA" if row % 2 else "white")

    ax.set_title(title or "Summary", fontsize=12, fontweight="bold", pad=14)
    return fig


# --------------------------------------------------------------------------- #
# Family E — carry attribution
# --------------------------------------------------------------------------- #


def fig_bucket_pnl_bars(df: pd.DataFrame, title: str = "") -> Figure | None:
    """h6 carry attribution: total PnL by days-since-rollover bucket."""
    needed = {"bucket", "net_pnl"}
    if not needed.issubset(df.columns):
        return None
    agg = df.groupby("bucket", sort=False)["net_pnl"].agg(["sum", "count", "mean"]).reset_index()

    fig, ax = plt.subplots(figsize=(11, 5))
    colors = [PALETTE["oos_pos"] if v >= 0 else PALETTE["oos_neg"] for v in agg["sum"]]
    bars = ax.bar(agg["bucket"], agg["sum"], color=colors, alpha=0.85)
    ax.axhline(0, color="#595959", linewidth=0.6)
    ax.set_ylabel("Total net PnL (CNY)")
    ax.set_title(title or "PnL by rollover bucket")
    for bar, n in zip(bars, agg["count"], strict=True):
        h = bar.get_height()
        ax.annotate(
            f"n={n}",
            xy=(bar.get_x() + bar.get_width() / 2, h),
            xytext=(0, 4 if h >= 0 else -12),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            color="#595959",
        )
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------- #
# Family B — ensemble fold results
# --------------------------------------------------------------------------- #


def fig_ensemble_vs_indiv(df: pd.DataFrame, title: str = "") -> Figure | None:
    """Ensemble OOS Sharpe per fold, with the indiv constituents as scatter.

    Lets you eyeball whether the top-k blend actually beats the best single
    member on each test window.
    """
    if "ens_oos_sharpe" not in df.columns or "fold" not in df.columns:
        return None

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.bar(
        df["fold"],
        df["ens_oos_sharpe"],
        color=PALETTE["oos"],
        alpha=0.7,
        label="Ensemble OOS Sharpe",
        width=0.6,
    )

    if "oos_sharpes_indiv_list" in df.columns:
        for _, row in df.iterrows():
            vals = row["oos_sharpes_indiv_list"]
            if isinstance(vals, list):
                ax.scatter(
                    [row["fold"]] * len(vals), vals, color=PALETTE["is"], s=30, alpha=0.85, zorder=3
                )
        ax.scatter([], [], color=PALETTE["is"], s=30, label="Individual member OOS Sharpe")

    ax.axhline(0, color="#595959", linewidth=0.6)
    ax.set_xlabel("Fold")
    ax.set_ylabel("OOS Sharpe")
    ax.set_title(title or "Ensemble vs individual OOS")
    ax.legend(loc="best")
    fig.tight_layout()
    return fig
