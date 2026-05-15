"""策略安全装饰器：捕获回调异常并通过 write_log 输出，由 NotifyListener 自动告警。"""

import logging
import traceback
from collections.abc import Callable
from functools import wraps

logger = logging.getLogger("strategy")


def safe_callback(func: Callable) -> Callable:
    """装饰策略回调（on_bar/on_tick），异常时 write_log 后吞掉，避免策略整个挂掉。"""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            if hasattr(self, "write_log"):
                self.write_log(f"[ERROR] {func.__name__} 异常: {e}\n{tb}")
            else:
                logger.error("%s 异常: %s\n%s", func.__name__, e, tb)

    return wrapper
