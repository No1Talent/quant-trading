"""Skip vn.py/plotly-dependent tests when those modules aren't installed.

vn.py ships with CTP DLLs that only build on Windows, so it cannot install on
Ubuntu CI. Tests that import vnpy (directly, or transitively via utils/ or
strategies/) are listed explicitly so additions stay visible to reviewers.
"""

from __future__ import annotations

import importlib.util

collect_ignore_glob: list[str] = []

if importlib.util.find_spec("vnpy") is None:
    collect_ignore_glob.extend(
        [
            "test_boll_reversal_strategy.py",
            "test_donchian_strategy.py",
            "test_double_ma_strategy.py",
            "test_import_data.py",
            "test_intraday_tick_strategy.py",
            "test_market_data.py",
            "test_market_intel.py",
            "test_market_watchlist.py",
            "test_notify_listener.py",
            "test_reconciler_flow.py",
            "test_reconciler_integration.py",
            "test_reconciler_logic.py",
            "test_replay_gateway.py",
            "test_risk_guard.py",
            "test_signal_log.py",
            "test_signal_only_gateway.py",
            "test_startup_reconcile_wiring.py",
            "test_strategy_base.py",
            "test_sync_data_loader.py",
        ]
    )

if importlib.util.find_spec("plotly") is None:
    collect_ignore_glob.append("test_candlestick.py")
