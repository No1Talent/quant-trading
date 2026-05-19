"""策略层公用工具：

- `safe_callback`：装饰 on_bar/on_tick 等回调，吞异常 + 写日志，避免单 bar 异常拖崩整个策略。
- `safe_buy/safe_sell/safe_short/safe_cover`：发单前调一遍 RiskGuard.check_order_pre()，
  通过才透传到 CtaTemplate.buy/...；被拒则 write_log 并返回空列表（与 vn.py 原生返回形态一致）。
  RiskGuard 未挂载（回测）时无 gate，自动透传。
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from functools import wraps
from typing import Any

from utils.risk_guard import get_active_risk_guard

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


def _gated_send(
    strategy: Any,
    method_name: str,
    price: float,
    volume: int,
    *args: Any,
    **kwargs: Any,
) -> list:
    """共用包装：gate → 透传到 strategy.<method_name>。

    `method_name` ∈ {"buy", "sell", "short", "cover"}，同时作为
    RiskGuard.check_order_pre 的 direction 标签使用（告警里直接读得到 buy/sell
    /short/cover 而不是开仓 / 平仓的一侧名）。其余位置 / 关键字参数（如
    `stop=True`、`lock=True`）原样转发，保持与 CtaTemplate 签名一致。
    """
    guard = get_active_risk_guard()
    if guard is not None:
        vt_symbol = getattr(strategy, "vt_symbol", "")
        allowed, reason = guard.check_order_pre(vt_symbol, method_name, price, volume)
        if not allowed:
            msg = (
                f"[RISK_GATE] {method_name} 被拒 vt_symbol={vt_symbol} "
                f"price={price} vol={volume} reason={reason}"
            )
            if hasattr(strategy, "write_log"):
                strategy.write_log(msg)
            else:
                logger.warning(msg)
            return []

    fn = getattr(strategy, method_name)
    return fn(price, volume, *args, **kwargs)


def safe_buy(strategy: Any, price: float, volume: int, *args: Any, **kwargs: Any) -> list:
    """gate + strategy.buy()。开多。"""
    return _gated_send(strategy, "buy", price, volume, *args, **kwargs)


def safe_sell(strategy: Any, price: float, volume: int, *args: Any, **kwargs: Any) -> list:
    """gate + strategy.sell()。平多。"""
    return _gated_send(strategy, "sell", price, volume, *args, **kwargs)


def safe_short(strategy: Any, price: float, volume: int, *args: Any, **kwargs: Any) -> list:
    """gate + strategy.short()。开空。"""
    return _gated_send(strategy, "short", price, volume, *args, **kwargs)


def safe_cover(strategy: Any, price: float, volume: int, *args: Any, **kwargs: Any) -> list:
    """gate + strategy.cover()。平空。"""
    return _gated_send(strategy, "cover", price, volume, *args, **kwargs)
