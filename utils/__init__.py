from .notifier import (
    INotifier,
    NotifyLevel,
    NullNotifier,
    WebhookNotifier,
    get_notifier,
    reset_notifier,
    set_notifier,
)
from .product_registry import (
    Product,
    ProductRegistry,
    RolloverConfig,
    get_default_registry,
    set_default_registry,
)
from .rollover import (
    DEFAULT_GAP_FLOOR_PCT,
    DEFAULT_OI_PCT_THRESHOLD,
    RolloverDetection,
    detect_rollover,
)
from .signal_log import (
    FileSignalLog,
    ISignalLog,
    NullSignalLog,
    get_signal_log,
    set_signal_log,
)

# The modules below all import vnpy, which is Windows-only (CTP DLL). Guarding
# the re-exports lets non-vnpy environments (CI on Ubuntu, doc builds) still
# import e.g. `utils.notifier` without dragging in the whole trading stack.
# Runtime consumers (vnpy_workspace/run.py) always have vnpy installed.
try:
    from .notify_listener import NotifyListener, attach_notify_listener
    from .reconciler import (
        CtpReconciler,
        ReconcileError,
        check_reconcile_flag,
        run_reconcile,
    )
    from .replay_gateway import ReplayGateway
    from .risk_guard import RiskGuard, attach_risk_guard, check_breach_flag
    from .signal_only_gateway import (
        SIGNAL_ORDERID_PREFIX,
        is_signal_trade,
        make_signal_only_class,
    )
    from .strategy_base import (
        install_live_eligibility_guard,
        is_live_eligible,
    )
    from .sync_data_loader import (
        aggregate_positions,
        load_local_positions_for_reconcile,
    )
except ImportError:
    pass

__all__ = [
    "INotifier",
    "WebhookNotifier",
    "NullNotifier",
    "NotifyLevel",
    "get_notifier",
    "set_notifier",
    "reset_notifier",
    "NotifyListener",
    "attach_notify_listener",
    "RiskGuard",
    "attach_risk_guard",
    "check_breach_flag",
    "CtpReconciler",
    "ReconcileError",
    "check_reconcile_flag",
    "run_reconcile",
    "aggregate_positions",
    "load_local_positions_for_reconcile",
    "make_signal_only_class",
    "is_signal_trade",
    "SIGNAL_ORDERID_PREFIX",
    "ReplayGateway",
    "install_live_eligibility_guard",
    "is_live_eligible",
    # P1 decouple — product / rollover / signal log
    "Product",
    "ProductRegistry",
    "RolloverConfig",
    "get_default_registry",
    "set_default_registry",
    "RolloverDetection",
    "detect_rollover",
    "DEFAULT_GAP_FLOOR_PCT",
    "DEFAULT_OI_PCT_THRESHOLD",
    "ISignalLog",
    "NullSignalLog",
    "FileSignalLog",
    "get_signal_log",
    "set_signal_log",
]
