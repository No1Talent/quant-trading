"""Watchlist loader for the Market Intel dashboard.

Reads a YAML file that lists the contracts the user cares about and the
display knobs for each (interval, n_bars, label). Used by streamlit_market.py
to render the multi-contract grid.

The file is treated as user-editable config — every field is validated
on load so a typo surfaces immediately rather than crashing the dashboard
mid-refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from utils.market_data import _INTERVAL_MAP

DEFAULT_WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "market_watchlist.yaml"


@dataclass(frozen=True)
class WatchItem:
    """One contract to display on the watchlist tab."""

    vt_symbol: str
    name: str
    interval: str
    n_bars: int


def load_watchlist(path: Path | str | None = None) -> list[WatchItem]:
    """Load and validate the watchlist YAML.

    Raises FileNotFoundError if the file is missing (don't silently fall back
    to an empty watchlist — that would mask a config typo). Raises ValueError
    on any structural problem with a message that points at the offending row.
    """
    p = Path(path) if path else DEFAULT_WATCHLIST_PATH
    if not p.exists():
        raise FileNotFoundError(f"watchlist YAML not found: {p}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if raw is None:
        return []

    if not isinstance(raw, dict) or "watchlist" not in raw:
        raise ValueError(f"{p}: top-level must be a dict with a 'watchlist' key")

    items_raw = raw["watchlist"]
    if not isinstance(items_raw, list):
        raise ValueError(f"{p}: 'watchlist' must be a list")

    items: list[WatchItem] = []
    for i, entry in enumerate(items_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{p}: item {i} is not a mapping")

        try:
            vt_symbol = str(entry["vt_symbol"])
            interval = str(entry["interval"])
            n_bars = int(entry["n_bars"])
            name = str(entry.get("name", vt_symbol))
        except KeyError as e:
            raise ValueError(f"{p}: item {i} missing required field {e}") from None

        if "." not in vt_symbol:
            raise ValueError(f"{p}: item {i} vt_symbol={vt_symbol!r} must be 'symbol.exchange'")
        if interval not in _INTERVAL_MAP:
            raise ValueError(f"{p}: item {i} interval={interval!r} not in {sorted(_INTERVAL_MAP)}")
        if n_bars <= 0:
            raise ValueError(f"{p}: item {i} n_bars must be positive, got {n_bars}")

        items.append(WatchItem(vt_symbol=vt_symbol, name=name, interval=interval, n_bars=n_bars))

    return items
