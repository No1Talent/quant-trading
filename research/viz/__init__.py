"""Decoupled visualization layer for the WFA research chain.

Three layers, all independent:
  - `themes`   : matplotlib rcParams + palette
  - `loaders`  : Path -> DataFrame (one per CSV family)
  - `charts`   : DataFrame -> matplotlib.Figure (pure, no I/O)

The `registry` module maps a CSV filename pattern to its
(loader, chart_set, label) so the renderer can dispatch automatically.
A later Streamlit/Panel front-end can reuse loaders + charts without
touching the renderer.
"""

from research.viz import charts, loaders, themes
from research.viz.registry import REGISTRY, dispatch

__all__ = ["charts", "loaders", "themes", "REGISTRY", "dispatch"]
