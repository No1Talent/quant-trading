"""matplotlib theme + colour palette for WFA figures.

Call `apply_theme()` once at process start (the renderer does this). Charts
themselves never touch rcParams — they read colours from `PALETTE` so the
same chart code works for batch-PNG and a future Streamlit shim.
"""

from __future__ import annotations

import matplotlib as mpl

PALETTE = {
    "is": "#5B8FF9",  # in-sample (blue)
    "oos": "#F6BD16",  # out-of-sample (amber)
    "oos_pos": "#52C41A",  # OOS positive Sharpe
    "oos_neg": "#E8684A",  # OOS negative Sharpe
    "portfolio": "#262626",
    "instruments": ["#5B8FF9", "#F6BD16", "#5AD8A6", "#E8684A", "#9270CA", "#5D7092"],
    "drawdown": "#E8684A",
    "grid": "#E5E5E5",
    "diagonal": "#BFBFBF",
}


def apply_theme() -> None:
    """Set global matplotlib defaults. Idempotent."""
    mpl.use("Agg")  # non-interactive backend; required for headless rendering
    mpl.rcParams.update(
        {
            "figure.dpi": 110,
            "savefig.dpi": 140,
            "savefig.bbox": "tight",
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": "#262626",
            "axes.labelcolor": "#262626",
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.labelsize": 10,
            "axes.grid": True,
            "grid.color": PALETTE["grid"],
            "grid.linewidth": 0.6,
            "xtick.color": "#595959",
            "ytick.color": "#595959",
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans", "Microsoft YaHei", "SimHei", "Arial"],
            "axes.unicode_minus": False,
        }
    )
