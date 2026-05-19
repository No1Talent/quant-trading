"""SIGNAL_ONLY 模式：拦截 gateway.send_order，合成回报但不下真单。

设计目标
--------
让 *同一份策略源码*（同一份回测口径）以"只出信号、不报单"的方式跑在实盘行情上：
- 实盘 Tick/Bar 喂给策略
- 策略 self.buy() → cta_engine.send_order → gateway.send_order
- **此处拦截**：合成 ALLTRADED OrderData + TradeData，把状态喂回策略，但绝不
  调用真实柜台的 td_api
- 同时给运营者推一条"信号触发"通知，由人工决定是否实盘下单

为什么要同步派发 (synchronous dispatch)
---------------------------------------
vn.py 的 EventEngine 是单线程 FIFO。若把合成 trade 走 event_engine.put()，
当前 on_tick 回调还没返回，队列里早就堆着后续 TICK；这些 TICK 会在 TRADE
事件之前被处理，导致策略下一秒看到 self.pos 仍为 0 → 重复满足开仓条件 →
Telegram 被刷爆（Gemini 指出的真 bug）。

解决：合成事件**绕过队列**，在 send_order 同一栈帧内直接调用 EventEngine 已注册
的处理器。这些 handler 本来就在 EventEngine 工作线程上跑，而我们也已经在该线程
上（因为 on_tick → ... → send_order 全链路同步），所以没破坏线程模型。

Handler 合约 (Handler contract)
-------------------------------
同步派发意味着 EVENT_ORDER / EVENT_TRADE 的所有 handler 都跑在策略主调用栈上。
**任何在 handler 内做同步阻塞 I/O 的代码都会卡死 tick 线程**。约束：

- ❌ 禁止：requests.post()、smtp.send_message()、psycopg2 同步 query、socket.recv()
- ✅ 允许：纯 CPU 操作；fire-and-forget 入队（如 ThreadPoolExecutor.submit）

`utils.notifier.WebhookNotifier` 把真实 HTTP/SMTP 投递扔进 ThreadPoolExecutor，
满足该合约。新增 handler 必须遵守同样规则。

兜底：`_dispatch_sync` 内置 watchdog，单个 handler 同步耗时超 100ms 即写
WARN 日志（不中断派发），便于运行期发现破坏合约的代码。

合成事件标记
-----------
合成的 Order/Trade 上有两个标记，两者满足任一即视为合成：
- `obj.is_virtual = True` — 强类型属性标记（首选）
- `orderid.startswith("signal_")` — 字符串前缀（向后兼容、跨进程序列化兜底）

调用方应使用 `is_signal_trade(obj)`，而不是直接看 orderid。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Status
from vnpy.trader.event import EVENT_ORDER, EVENT_TRADE
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import CancelRequest, OrderData, OrderRequest, TradeData

from .notifier import INotifier, NotifyLevel, get_notifier

if TYPE_CHECKING:
    pass

logger = logging.getLogger("signal_only_gateway")

# orderid 前缀 — NotifyListener 据此跳过假成交，避免与 _notify_signal 重复推送
SIGNAL_ORDERID_PREFIX = "signal_"

# 单 handler 同步执行超过此阈值即触发 watchdog 警告（见 Handler 合约 docstring）
_HANDLER_SLOW_THRESHOLD_MS = 100.0


def is_signal_trade(trade_or_order) -> bool:
    """供 NotifyListener / RiskGuard 判断是否合成事件。

    双校验：is_virtual 属性（强类型，首选）或 orderid 前缀（向后兼容兜底）。
    """
    if getattr(trade_or_order, "is_virtual", False):
        return True
    oid = getattr(trade_or_order, "orderid", "") or ""
    return oid.startswith(SIGNAL_ORDERID_PREFIX)


def make_signal_only_class(real_gateway_cls: type[BaseGateway]) -> type[BaseGateway]:
    """工厂：给任意 vnpy gateway 套一层 SIGNAL_ONLY 拦截。

    用法
    ----
        SignalCtp = make_signal_only_class(CtpGateway)
        main_engine.add_gateway(SignalCtp)   # gateway_name 仍是 "CTP"

    被覆盖的方法
    ------------
    - send_order: 不下真单，合成 ALLTRADED + Trade 事件同步派发
    - cancel_order: 假成交瞬时完成，无活动单，no-op

    保留的方法
    ----------
    - connect / subscribe / close / query_account / query_position / write_log ...
      全部继承父类，行情链路与真实 gateway 一致。
    """

    class _SignalOnlyGateway(real_gateway_cls):
        signal_only_mode: bool = True

        def __init__(self, event_engine: EventEngine, gateway_name: str = "") -> None:
            super().__init__(event_engine, gateway_name)
            self._signal_orderid_seq = 0
            self._signal_tradeid_seq = 0
            self._signal_notifier: INotifier | None = None
            logger.warning(
                "SIGNAL_ONLY 模式启用 — gateway=%s 将拦截所有 send_order，不下真单",
                gateway_name,
            )

        def set_signal_notifier(self, notifier: INotifier) -> None:
            """显式注入通知器；不调用则在首次 send_order 时走 get_notifier()。"""
            self._signal_notifier = notifier

        # ------------------------------------------------------------------
        # 拦截点
        # ------------------------------------------------------------------

        def send_order(self, req: OrderRequest) -> str:
            orderid = self._next_signal_orderid()
            vt_orderid = f"{self.gateway_name}.{orderid}"
            now = datetime.now()

            order = OrderData(
                gateway_name=self.gateway_name,
                symbol=req.symbol,
                exchange=req.exchange,
                orderid=orderid,
                type=req.type,
                direction=req.direction,
                offset=req.offset,
                price=req.price,
                volume=req.volume,
                traded=req.volume,
                status=Status.ALLTRADED,
                datetime=now,
                reference=req.reference,
            )
            # 强类型标记：is_signal_trade 优先识别此属性；orderid 前缀做二级兜底
            order.is_virtual = True
            trade = TradeData(
                gateway_name=self.gateway_name,
                symbol=req.symbol,
                exchange=req.exchange,
                orderid=orderid,
                tradeid=self._next_signal_tradeid(),
                direction=req.direction,
                offset=req.offset,
                price=req.price,
                volume=req.volume,
                datetime=now,
            )
            trade.is_virtual = True
            # TradeData 字段定义没有 reference，但允许 attribute 后置赋值
            # （CtaEngine 的真实成交路径也常这么做）
            trade.reference = req.reference

            # 同步派发：见模块 docstring。必须在 _notify_signal 之前完成，
            # 这样即使 notifier 异常，策略状态也已正确锁定。
            self._dispatch_sync(EVENT_ORDER, order)
            self._dispatch_sync(EVENT_TRADE, trade)

            # 给运营者的"信号触发"提示 — 与策略内部状态机解耦
            self._notify_signal(req)

            return vt_orderid

        def cancel_order(self, req: CancelRequest) -> None:
            # 假成交瞬时完成，没有活动单可撤
            logger.debug("SIGNAL_ONLY: cancel_order ignored for %s", req.orderid)

        # ------------------------------------------------------------------
        # 内部
        # ------------------------------------------------------------------

        def _next_signal_orderid(self) -> str:
            self._signal_orderid_seq += 1
            return f"{SIGNAL_ORDERID_PREFIX}{self._signal_orderid_seq:08d}"

        def _next_signal_tradeid(self) -> str:
            self._signal_tradeid_seq += 1
            return f"{SIGNAL_ORDERID_PREFIX}t{self._signal_tradeid_seq:08d}"

        def _dispatch_sync(self, event_type: str, data) -> None:
            event = Event(type=event_type, data=data)
            handlers = list(self.event_engine._handlers.get(event_type, []))
            general = list(getattr(self.event_engine, "_general_handlers", []))
            for handler in handlers + general:
                t0 = time.perf_counter()
                try:
                    handler(event)
                except Exception:
                    # 单个 handler 异常不应阻断订单状态机
                    logger.exception("handler error for %s", event_type)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if elapsed_ms > _HANDLER_SLOW_THRESHOLD_MS:
                    # Watchdog：handler 在同步路径上不允许做阻塞 I/O。
                    # 不中断派发（已不可逆），仅记 WARN 便于事后定位破坏合约的代码。
                    logger.warning(
                        "SIGNAL_ONLY handler %s 处理 %s 同步耗时 %.1fms — "
                        "见 Handler contract docstring，疑似阻塞 I/O",
                        getattr(handler, "__qualname__", repr(handler)),
                        event_type,
                        elapsed_ms,
                    )

        def _notify_signal(self, req: OrderRequest) -> None:
            notifier = self._signal_notifier or get_notifier()
            arrow = "🟢" if req.direction.value == "多" else "🔴"
            msg = (
                f"{arrow} 信号触发（未实盘）\n"
                f"━━━━━━━━━━━━━━\n"
                f"策略：{req.reference or 'N/A'}\n"
                f"合约：{req.symbol}.{req.exchange.value}\n"
                f"方向：{req.direction.value} {req.offset.value}\n"
                f"价格：{req.price}\n"
                f"数量：{req.volume}手\n"
                f"模式：SIGNAL_ONLY（未真实下单）"
            )
            try:
                notifier.send(
                    msg,
                    title=f"信号-{req.reference or '?'}",
                    level=NotifyLevel.WARNING,
                    force=True,
                )
            except Exception:
                logger.exception("notify signal failed")

    _SignalOnlyGateway.__name__ = f"SignalOnly{real_gateway_cls.__name__}"
    _SignalOnlyGateway.__qualname__ = _SignalOnlyGateway.__name__
    return _SignalOnlyGateway
