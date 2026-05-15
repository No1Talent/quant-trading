"""事件订阅式通知监听器：订阅 vn.py 事件总线，策略代码只用 write_log。"""

import logging

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Status
from vnpy.trader.event import (
    EVENT_ACCOUNT,
    EVENT_LOG,
    EVENT_ORDER,
    EVENT_TRADE,
)

# CTA事件
try:
    from vnpy_ctastrategy.base import EVENT_CTA_LOG, EVENT_CTA_STRATEGY
except ImportError:
    EVENT_CTA_LOG = "eCtaLog"
    EVENT_CTA_STRATEGY = "eCtaStrategy"

from .notifier import INotifier, NotifyLevel, get_notifier

logger = logging.getLogger("notify_listener")


class NotifyListener:
    """订阅事件总线，独立完成通知推送。在 run.py 调用 attach_notify_listener。"""

    CRITICAL_KEYWORDS = [
        "断线",
        "断开",
        "连接失败",
        "登录失败",
        "网络异常",
        "ConnectionError",
        "Connection refused",
        "CTP:行情前置不活跃",
        "CTP:交易前置不活跃",
    ]

    WARNING_KEYWORDS = [
        "错误",
        "异常",
        "失败",
        "拒绝",
        "Error",
        "Exception",
        "Failed",
        "撤单失败",
        "下单失败",
    ]

    def __init__(
        self,
        main_engine,
        event_engine: EventEngine,
        notifier: INotifier | None = None,
        balance_alarm_pct: float = 0.05,
    ):
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.notifier = notifier or get_notifier()

        self.last_balance: float | None = None
        self.balance_alarm_pct = balance_alarm_pct
        self.strategy_status: dict[str, str] = {}

        self._register()

        self.notifier.send(
            "系统监控已启动\n监听: 日志、订单、成交、账户、策略状态",
            title="系统监控",
            level=NotifyLevel.INFO,
            force=True,
        )

    def _register(self):
        self.event_engine.register(EVENT_LOG, self.on_log)
        self.event_engine.register(EVENT_ORDER, self.on_order)
        self.event_engine.register(EVENT_TRADE, self.on_trade)
        self.event_engine.register(EVENT_ACCOUNT, self.on_account)
        self.event_engine.register(EVENT_CTA_LOG, self.on_cta_log)
        self.event_engine.register(EVENT_CTA_STRATEGY, self.on_cta_strategy)

    def unregister(self):
        self.event_engine.unregister(EVENT_LOG, self.on_log)
        self.event_engine.unregister(EVENT_ORDER, self.on_order)
        self.event_engine.unregister(EVENT_TRADE, self.on_trade)
        self.event_engine.unregister(EVENT_ACCOUNT, self.on_account)
        self.event_engine.unregister(EVENT_CTA_LOG, self.on_cta_log)
        self.event_engine.unregister(EVENT_CTA_STRATEGY, self.on_cta_strategy)

    def on_log(self, event: Event):
        log = event.data
        msg = log.msg if hasattr(log, "msg") else str(log)

        # 防止递归：跳过 Notifier / Listener 自己写的日志，否则告警会无限触发自身。
        if "[Notifier]" in msg or "[NotifyListener]" in msg:
            return

        gateway_name = getattr(log, "gateway_name", "")
        if gateway_name in ("Notifier", "NotifyListener"):
            return

        # 关键词匹配
        for kw in self.CRITICAL_KEYWORDS:
            if kw in msg:
                self.notifier.send_critical(
                    f"系统严重事件\n来源：{gateway_name or 'system'}\n内容：{msg}"
                )
                return

        for kw in self.WARNING_KEYWORDS:
            if kw in msg:
                self.notifier.send(
                    f"系统警告\n来源：{gateway_name or 'system'}\n内容：{msg}",
                    title="系统警告",
                    level=NotifyLevel.WARNING,
                )
                return

    def on_cta_log(self, event: Event):
        self.on_log(event)

    def on_order(self, event: Event):
        order = event.data
        if order.status == Status.REJECTED:
            self.notifier.send(
                f"订单被拒\n"
                f"合约：{order.vt_symbol}\n"
                f"方向：{order.direction.value} {order.offset.value}\n"
                f"价格：{order.price} 数量：{order.volume}\n"
                f"策略：{order.reference or '未知'}",
                title="拒单告警",
                level=NotifyLevel.WARNING,
            )

    def on_trade(self, event: Event):
        trade = event.data
        self.notifier.send_trade(
            trade.reference or "未知策略",
            {
                "symbol": trade.vt_symbol,
                "direction": trade.direction.value,
                "offset": trade.offset.value,
                "price": trade.price,
                "volume": trade.volume,
                "datetime": trade.datetime,
            },
        )

    def on_account(self, event: Event):
        account = event.data

        if self.last_balance is None:
            self.last_balance = account.balance
            self.notifier.send(
                f"账户已连接\n账户：{account.accountid}\n"
                f"余额：{account.balance:.2f}\n"
                f"可用：{account.available:.2f}",
                title="账户连接",
                level=NotifyLevel.INFO,
            )
            return

        if self.last_balance > 0:
            change_pct = abs(account.balance - self.last_balance) / self.last_balance
            if change_pct >= self.balance_alarm_pct:
                direction = "+" if account.balance > self.last_balance else "-"
                self.notifier.send(
                    f"账户余额大幅变化\n"
                    f"上次：{self.last_balance:.2f}\n"
                    f"当前：{account.balance:.2f}\n"
                    f"变化：{direction}{change_pct * 100:.2f}%",
                    title="账户监控",
                    level=NotifyLevel.WARNING,
                    force=True,
                )
                self.last_balance = account.balance

    def on_cta_strategy(self, event: Event):
        data = event.data
        if not isinstance(data, dict):
            return

        strategy_name = data.get("strategy_name", "")
        if not strategy_name:
            return

        # data 结构示例: {"strategy_name": ..., "inited": True, "trading": True, ...}
        new_status = (
            "运行中" if data.get("trading") else ("已初始化" if data.get("inited") else "未初始化")
        )

        old_status = self.strategy_status.get(strategy_name)
        if old_status != new_status:
            self.strategy_status[strategy_name] = new_status

            if old_status == "已初始化" and new_status == "运行中":
                self.notifier.send(
                    f"策略已启动\n名称：{strategy_name}\n"
                    f"合约：{data.get('vt_symbol', 'N/A')}\n"
                    f"持仓：{data.get('pos', 0)}",
                    title=f"启动-{strategy_name}",
                    level=NotifyLevel.INFO,
                )
            elif old_status == "运行中" and new_status in ("已初始化", "未初始化"):
                self.notifier.send(
                    f"策略已停止\n名称：{strategy_name}\n持仓：{data.get('pos', 0)}",
                    title=f"停止-{strategy_name}",
                    level=NotifyLevel.WARNING,
                    force=True,
                )


# 模块级列表持有监听器引用，防止被 GC 回收
_listeners: list = []


def attach_notify_listener(
    main_engine, event_engine: EventEngine, notifier: INotifier | None = None
) -> NotifyListener:
    """挂载监听器，在 run.py 调用一次即可。"""
    listener = NotifyListener(main_engine, event_engine, notifier)
    _listeners.append(listener)
    return listener
