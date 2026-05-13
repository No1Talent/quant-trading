"""
================================================================
策略安全装饰器
================================================================
本模块替代原来的 NotifyMixin。设计原则：
    - 策略代码与通知系统完全解耦
    - 只提供异常隔离能力，通知由NotifyListener独立处理
    - 不依赖MRO，不强制继承关系

用法：
    from utils.strategy_base import safe_callback

    class MyStrategy(CtaTemplate):
        @safe_callback
        def on_bar(self, bar):
            # 异常会被自动捕获并通过write_log触发
            # NotifyListener监听到日志会自动推送告警
            ...
================================================================
"""

import logging
import traceback
from collections.abc import Callable
from functools import wraps

logger = logging.getLogger("strategy")


def safe_callback(func: Callable) -> Callable:
    """
    装饰器：包装策略回调函数

    作用：
        1. 捕获异常防止策略崩溃
        2. 异常信息通过策略的write_log输出（触发LOG事件）
        3. NotifyListener会自动监听LOG事件中的错误关键词并推送

    Args:
        func: 被装饰的方法

    Returns:
        包装后的方法
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            # 通过策略的write_log输出，会触发LOG事件
            # NotifyListener会捕获并推送
            if hasattr(self, "write_log"):
                self.write_log(f"[ERROR] {func.__name__} 异常: {e}\n{tb}")
            else:
                logger.error("%s 异常: %s\n%s", func.__name__, e, tb)
            # 不重新抛出，让策略继续运行

    return wrapper
