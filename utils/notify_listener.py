"""
================================================================
通知监听器（事件订阅模式）
================================================================
本模块替代原来的 NotifyMixin，彻底解决：
    SEVERE-6: Mixin依赖MRO，子类重写on_*时容易漏调super()
    OPT-1:   策略与Notifier强耦合，无法独立测试/回测

设计思路：
    策略代码不再import Notifier，只发vn.py标准事件
    监听器在外部订阅这些事件，独立完成推送
    回测时不挂载监听器，零副作用

策略代码只需：
    self.write_log("信号: 金叉做多")    # 日志会触发LOG事件

监听器自动处理：
    - 启动/停止通知（通过策略状态变化）
    - 成交回报通知（订阅EVENT_TRADE）
    - 拒单告警（订阅EVENT_ORDER）
    - 异常告警（订阅EVENT_CTA_LOG中的ERROR级别）
================================================================
"""

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
    """
    通知监听器 - 订阅事件总线，独立完成通知推送

    使用方法（在run.py里）：
        from utils.notify_listener import NotifyListener
        listener = NotifyListener(main_engine, event_engine)
        # 之后所有事件自动推送通知，策略代码不用任何改动
    """

    # 严重错误关键词
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

    # 一般警告关键词
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
        """
        Args:
            main_engine: vn.py主引擎
            event_engine: 事件引擎
            notifier: 通知器实例，None则用全局单例
            balance_alarm_pct: 账户余额变化告警阈值
        """
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.notifier = notifier or get_notifier()

        # 账户监控状态
        self.last_balance: float | None = None
        self.balance_alarm_pct = balance_alarm_pct

        # 策略状态缓存（监控启停）
        self.strategy_status: dict[str, str] = {}

        # 注册事件
        self._register()

        self.notifier.send(
            "系统监控已启动\n监听: 日志、订单、成交、账户、策略状态",
            title="系统监控",
            level=NotifyLevel.INFO,
            force=True,
        )

    def _register(self):
        """注册所有事件handler"""
        self.event_engine.register(EVENT_LOG, self.on_log)
        self.event_engine.register(EVENT_ORDER, self.on_order)
        self.event_engine.register(EVENT_TRADE, self.on_trade)
        self.event_engine.register(EVENT_ACCOUNT, self.on_account)
        self.event_engine.register(EVENT_CTA_LOG, self.on_cta_log)
        self.event_engine.register(EVENT_CTA_STRATEGY, self.on_cta_strategy)

    def unregister(self):
        """注销所有handler（测试或重载时用）"""
        self.event_engine.unregister(EVENT_LOG, self.on_log)
        self.event_engine.unregister(EVENT_ORDER, self.on_order)
        self.event_engine.unregister(EVENT_TRADE, self.on_trade)
        self.event_engine.unregister(EVENT_ACCOUNT, self.on_account)
        self.event_engine.unregister(EVENT_CTA_LOG, self.on_cta_log)
        self.event_engine.unregister(EVENT_CTA_STRATEGY, self.on_cta_strategy)

    # ========================================================
    # 事件处理函数
    # ========================================================

    def on_log(self, event: Event):
        """
        全局日志事件 - 监控错误关键词
        SEVERE-5: 跳过Notifier自身的日志，防止递归
        """
        log = event.data
        msg = log.msg if hasattr(log, "msg") else str(log)

        # 防止递归：跳过Notifier和Listener自身的日志
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
        """CTA策略日志 - 复用全局日志的关键词逻辑"""
        self.on_log(event)

    def on_order(self, event: Event):
        """订单事件 - 拒单告警"""
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
        """成交回报 - 自动推送给所有策略"""
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
        """账户事件 - 监控资金大幅变化"""
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
        """
        CTA策略状态变化 - 监控启停
        替代原来需要在策略on_start/on_stop里写的通知代码
        """
        data = event.data
        if not isinstance(data, dict):
            return

        strategy_name = data.get("strategy_name", "")
        if not strategy_name:
            return

        # 检测状态变化
        # data结构示例: {"strategy_name": ..., "inited": True, "trading": True, ...}
        new_status = (
            "运行中" if data.get("trading") else ("已初始化" if data.get("inited") else "未初始化")
        )

        old_status = self.strategy_status.get(strategy_name)
        if old_status != new_status:
            self.strategy_status[strategy_name] = new_status

            # 只在重要状态切换时推送
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


# 模块级列表持有监听器引用，防止被GC回收
_listeners: list = []


def attach_notify_listener(
    main_engine, event_engine: EventEngine, notifier: INotifier | None = None
) -> NotifyListener:
    """
    便捷函数：挂载监听器
    在run.py里调用一次即可
    """
    listener = NotifyListener(main_engine, event_engine, notifier)
    _listeners.append(listener)
    return listener
