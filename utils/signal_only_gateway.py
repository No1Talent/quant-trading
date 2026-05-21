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

兜底：`dispatch_sync` 内置 watchdog，单个 handler 同步耗时超 100ms 即写
WARN 日志（不中断派发），便于运行期发现破坏合约的代码。

合成事件标记
-----------
合成的 Order/Trade 上有两个标记，两者满足任一即视为合成：
- `obj.is_virtual = True` — 强类型属性标记（首选）
- `orderid.startswith("signal_")` — 字符串前缀（向后兼容、跨进程序列化兜底）

调用方应使用 `is_signal_trade(obj)`，而不是直接看 orderid。

模块结构 (2026-05-19 拆分)
--------------------------
顶层 free functions 是 SIGNAL_ONLY / REPLAY 共用的"合成原语"：
- ``synthesize_order_trade(...)`` 构造一对 Order+Trade（含 is_virtual 标记）
- ``dispatch_sync(event_engine, ...)`` 同步派发 + watchdog
- ``notify_signal(...)`` 通知模板，``mode_label`` 区分 "SIGNAL_ONLY" / "REPLAY"
- ``OrderIdSequencer`` 线程安全自增 orderid/tradeid

``make_signal_only_class`` 仅是把这些原语装进 CtpGateway 子类的薄外壳；
``utils.replay_gateway.ReplayGateway`` 用同样的原语驱动 DB 回放。
"""

from __future__ import annotations

import logging
import threading
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


# ----------------------------------------------------------------------
# 共用合成原语（SIGNAL_ONLY 与 REPLAY 共享）
# ----------------------------------------------------------------------


class OrderIdSequencer:
    """单调自增的合成 orderid / tradeid 生成器。

    用法
    ----
        seq = OrderIdSequencer()
        oid = seq.next_orderid()  # "signal_00000001"
        tid = seq.next_tradeid()  # "signal_t00000001"

    线程安全；ReplayGateway 的回放线程和 send_order 调用栈可能交叉访问。
    """

    def __init__(self) -> None:
        self._orderid_seq = 0
        self._tradeid_seq = 0
        self._lock = threading.Lock()

    def next_orderid(self) -> str:
        with self._lock:
            self._orderid_seq += 1
            return f"{SIGNAL_ORDERID_PREFIX}{self._orderid_seq:08d}"

    def next_tradeid(self) -> str:
        with self._lock:
            self._tradeid_seq += 1
            return f"{SIGNAL_ORDERID_PREFIX}t{self._tradeid_seq:08d}"


def synthesize_order_trade(
    req: OrderRequest,
    gateway_name: str,
    orderid: str,
    tradeid: str,
    now: datetime | None = None,
) -> tuple[OrderData, TradeData]:
    """从 OrderRequest 合成 ALLTRADED OrderData + TradeData 对。

    两者都带 ``is_virtual = True`` 标记，``orderid`` 前缀 ``signal_`` 兜底。
    """
    if now is None:
        now = datetime.now()

    order = OrderData(
        gateway_name=gateway_name,
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
    order.is_virtual = True

    trade = TradeData(
        gateway_name=gateway_name,
        symbol=req.symbol,
        exchange=req.exchange,
        orderid=orderid,
        tradeid=tradeid,
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

    return order, trade


def synthesize_rejection(
    req: OrderRequest,
    gateway_name: str,
    orderid: str,
    now: datetime | None = None,
    reason: str = "synthetic reject",
) -> OrderData:
    """从 OrderRequest 合成一个 ``status=REJECTED`` 的 OrderData（无 Trade）。

    用于 SIT M0 负路径：模拟柜台直接拒单（资金不足 / 价格越界 / 风控前置等），
    验证下游策略状态机 + RiskGuard 在没有 Trade 事件的前提下不出现仓位漂移、
    不重复发单。``is_virtual=True`` 标记仍保留，方便 NotifyListener 跳过假成交。
    """
    if now is None:
        now = datetime.now()
    order = OrderData(
        gateway_name=gateway_name,
        symbol=req.symbol,
        exchange=req.exchange,
        orderid=orderid,
        type=req.type,
        direction=req.direction,
        offset=req.offset,
        price=req.price,
        volume=req.volume,
        traded=0,
        status=Status.REJECTED,
        datetime=now,
        reference=req.reference,
    )
    order.is_virtual = True
    order.reject_reason = reason  # 自由附加字段，便于测试断言
    return order


def synthesize_partial_fill_sequence(
    req: OrderRequest,
    gateway_name: str,
    orderid: str,
    tradeid_prefix: str,
    fills: list[int],
    now: datetime | None = None,
) -> tuple[list[OrderData], list[TradeData]]:
    """合成一系列"部分成交→部分成交→...→全部成交"的 Order/Trade 流。

    ``fills`` 是每笔 trade 的成交量（按时间顺序）；``sum(fills)`` 必须 == ``req.volume``。
    每笔 trade 之前对应一次 OrderData 推送：状态在 ``Status.PARTTRADED`` 与
    ``Status.ALLTRADED`` 之间根据累计已成交量切换。tradeid 用 ``f"{prefix}_{i}"``。

    返回 (orders, trades)，长度相等。调用方按 (order[i], trade[i]) 配对依次派发。

    Why a single helper instead of one-shot trades
    ----------------------------------------------
    CtaEngine 的 ``process_trade_event`` 按 ``vt_tradeid`` 去重（line 198-200）：
    多笔不同 tradeid 的 TradeData 都会调用 ``strategy.pos += trade.volume``。所以
    部分成交累积是天然支持的，但 OrderData 的状态机也必须正确演进 ——
    PARTTRADED 时 ``is_active()`` 仍 True（订单挂着），ALLTRADED 时才 False
    （从 strategy_orderid_map 删掉）。把"order 状态 + trade 配对"绑在一起合成，
    SIT 测试就不会因为忘掉某一边而出现误报。
    """
    if sum(fills) != req.volume:
        raise ValueError(
            f"synthesize_partial_fill_sequence: sum(fills)={sum(fills)} 不等于 "
            f"req.volume={req.volume}"
        )
    if not fills:
        raise ValueError("synthesize_partial_fill_sequence: fills 不能为空")
    if now is None:
        now = datetime.now()

    orders: list[OrderData] = []
    trades: list[TradeData] = []
    cumulative = 0
    for i, vol in enumerate(fills):
        if vol <= 0:
            raise ValueError(f"synthesize_partial_fill_sequence: fills[{i}]={vol} 非正数")
        cumulative += vol
        is_final = cumulative == req.volume
        order = OrderData(
            gateway_name=gateway_name,
            symbol=req.symbol,
            exchange=req.exchange,
            orderid=orderid,
            type=req.type,
            direction=req.direction,
            offset=req.offset,
            price=req.price,
            volume=req.volume,
            traded=cumulative,
            status=Status.ALLTRADED if is_final else Status.PARTTRADED,
            datetime=now,
            reference=req.reference,
        )
        order.is_virtual = True
        trade = TradeData(
            gateway_name=gateway_name,
            symbol=req.symbol,
            exchange=req.exchange,
            orderid=orderid,
            tradeid=f"{tradeid_prefix}_{i}",
            direction=req.direction,
            offset=req.offset,
            price=req.price,
            volume=vol,
            datetime=now,
        )
        trade.is_virtual = True
        trade.reference = req.reference
        orders.append(order)
        trades.append(trade)
    return orders, trades


def find_cta_engine(event_engine: EventEngine):
    """从 EventEngine 已注册的 EVENT_ORDER handler 反查 CtaEngine 实例。

    通过 handler 反查而不是 ``main_engine.get_engine`` —— gateway 没有 main_engine
    引用，但有 event_engine。``CtaEngine.register_event`` 把 ``process_order_event``
    注册到 EVENT_ORDER，所以 ``handler.__self__`` 就是 CtaEngine 实例。

    返回 None 表示当前 EventEngine 上没有 CtaEngine —— 例如裸 unit test 场景，
    调用方据此回退到"立即同步派发"路径。
    """
    try:
        from vnpy_ctastrategy import CtaEngine
    except ImportError:
        return None
    handlers = event_engine._handlers.get(EVENT_ORDER, [])
    for handler in handlers:
        target = getattr(handler, "__self__", None)
        if isinstance(target, CtaEngine):
            return target
    return None


def cta_orderid_pending_or_dispatch(
    event_engine: EventEngine,
    req: OrderRequest,
    vt_orderid: str,
    events: list,
    pending_buffer: list,
) -> None:
    """Decide between deferred-flush and immediate sync-dispatch for synthesized events.

    Why this exists
    ---------------
    vn.py ``CtaEngine.send_server_order`` 的代码顺序是：

        1. ``main_engine.send_order(req)`` → ``gateway.send_order(req)`` 返回 vt_orderid
        2. ``orderid_strategy_map[vt_orderid] = strategy``
        3. ``strategy_orderid_map[strategy_name].add(vt_orderid)``

    实盘 CTP 异步回报，Order/Trade 走 ``event_engine.put`` 队列，worker 线程稍后才
    处理，那时第 2/3 步早已完成 —— 无竞争。

    SIGNAL_ONLY / REPLAY 在第 1 步内部就 ``dispatch_sync`` 合成的 Order+Trade，调用
    栈尚未回到第 2 步：
    - ``process_order_event`` / ``process_trade_event`` 找不到 strategy → 静默 return →
      策略永远收不到 on_order / on_trade，``self.pos`` 卡 0。
    - 即使提前 plant orderid → strategy 映射，``process_order_event`` 看到 REJECTED /
      ALLTRADED 会从 ``strategy_orderid_map`` 移除 vt_orderid，但第 3 步 ``set.add``
      又把它加回去，"active 集"漂出未结清的死单 → ``cancel_all`` 反复刷"委托撤单"。

    Fix
    ---
    如果当前 EventEngine 挂着 CtaEngine（生产环境 / 集成测试），把合成事件 ``append``
    到 ``pending_buffer``：调用方（``ReplayGateway._replay_loop`` 或 ``safe_*`` 包装
    里的 ``_flush_signal_gateway_pending``）会在 ``send_server_order`` 完成"第 3 步"
    **之后**显式 ``flush_pending_dispatches``，那时 ``strategy_orderid_map`` 已经包含
    vt_orderid，``process_order_event`` 的状态机移除生效，cta_engine 不再追加。

    如果当前 EventEngine **不挂** CtaEngine（裸 unit test），等不到第三方 flush —— 直接
    ``dispatch_sync`` 把 events 派给 RiskGuard / NotifyListener / 测试 spy 这些非
    cta-engine handler。
    """
    if find_cta_engine(event_engine) is not None:
        pending_buffer.extend(events)
    else:
        for event_type, data in events:
            dispatch_sync(event_engine, event_type, data)


def dispatch_sync(event_engine: EventEngine, event_type: str, data) -> None:
    """同步派发事件到 EventEngine 已注册的 handler，绕过 FIFO 队列。

    见模块顶部 "Handler 合约"。单 handler 抛异常被吞掉（记 logger.exception），
    单 handler 同步耗时超 100ms 触发 watchdog 警告（不中断派发）。
    """
    event = Event(type=event_type, data=data)
    handlers = list(event_engine._handlers.get(event_type, []))
    general = list(getattr(event_engine, "_general_handlers", []))
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


def notify_signal(
    notifier: INotifier,
    req: OrderRequest,
    mode_label: str = "SIGNAL_ONLY（未真实下单）",
) -> None:
    """给运营者推"信号触发"提示。``mode_label`` 区分 SIGNAL_ONLY / REPLAY。

    notifier.send 抛异常被吞掉 — 通知失败不能阻断订单状态机。
    """
    arrow = "🟢" if req.direction.value == "多" else "🔴"
    msg = (
        f"{arrow} 信号触发（未实盘）\n"
        f"━━━━━━━━━━━━━━\n"
        f"策略：{req.reference or 'N/A'}\n"
        f"合约：{req.symbol}.{req.exchange.value}\n"
        f"方向：{req.direction.value} {req.offset.value}\n"
        f"价格：{req.price}\n"
        f"数量：{req.volume}手\n"
        f"模式：{mode_label}"
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


# ----------------------------------------------------------------------
# SIGNAL_ONLY gateway 工厂
# ----------------------------------------------------------------------


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
            self._id_seq = OrderIdSequencer()
            self._signal_notifier: INotifier | None = None
            # 见 cta_orderid_pending_or_dispatch docstring：CtaEngine 在场时这里
            # 攒事件等 _flush_signal_gateway_pending 调用 flush；不在场时直接同步派发。
            self._pending_dispatches: list = []
            logger.warning(
                "SIGNAL_ONLY 模式启用 — gateway=%s 将拦截所有 send_order，不下真单",
                gateway_name,
            )

        def set_signal_notifier(self, notifier: INotifier) -> None:
            """显式注入通知器；不调用则在首次 send_order 时走 get_notifier()。"""
            self._signal_notifier = notifier

        def flush_pending_dispatches(self) -> None:
            """把上一笔 send_order 攒下来的合成事件同步派发出去。

            ``utils.strategy_base._gated_send`` 在 ``strategy.<method>(...)`` 返回后
            立即调一次本方法 —— 此时 CtaEngine 已经在 ``send_server_order`` 第 333-334
            行 plant 完 orderid 映射，``process_order_event`` 拿得到策略且状态机移除
            生效。
            """
            pending = self._pending_dispatches
            self._pending_dispatches = []
            for event_type, data in pending:
                dispatch_sync(self.event_engine, event_type, data)

        # ------------------------------------------------------------------
        # 拦截点
        # ------------------------------------------------------------------

        def send_order(self, req: OrderRequest) -> str:
            orderid = self._id_seq.next_orderid()
            vt_orderid = f"{self.gateway_name}.{orderid}"
            order, trade = synthesize_order_trade(
                req,
                gateway_name=self.gateway_name,
                orderid=orderid,
                tradeid=self._id_seq.next_tradeid(),
            )

            # CtaEngine 在场 → 攒到 pending_buffer 等 _gated_send flush；不在场 →
            # 立即 dispatch_sync（裸 unit test / RiskGuard-only 路径）。详见
            # signal_only_gateway.cta_orderid_pending_or_dispatch docstring。
            cta_orderid_pending_or_dispatch(
                self.event_engine,
                req,
                vt_orderid,
                events=[(EVENT_ORDER, order), (EVENT_TRADE, trade)],
                pending_buffer=self._pending_dispatches,
            )

            # 给运营者的"信号触发"提示 — 与策略内部状态机解耦
            notify_signal(
                self._signal_notifier or get_notifier(),
                req,
                mode_label="SIGNAL_ONLY（未真实下单）",
            )

            return vt_orderid

        def cancel_order(self, req: CancelRequest) -> None:
            # 假成交瞬时完成，没有活动单可撤
            logger.debug("SIGNAL_ONLY: cancel_order ignored for %s", req.orderid)

    _SignalOnlyGateway.__name__ = f"SignalOnly{real_gateway_cls.__name__}"
    _SignalOnlyGateway.__qualname__ = _SignalOnlyGateway.__name__
    return _SignalOnlyGateway
