"""SignalOnlyGateway 单元测试。

核心目标：验证 Gemini 指出的 self.pos 陷阱在 SIGNAL_ONLY 模式下被同步派发机制
化解 —— 即 send_order 返回时，已注册到 EVENT_TRADE 的 handler 必须已被调用，
不能仅入队等待事件线程消费。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.event import EVENT_ORDER, EVENT_TRADE
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import OrderRequest

from utils.notifier import NullNotifier
from utils.signal_only_gateway import (
    SIGNAL_ORDERID_PREFIX,
    is_signal_trade,
    make_signal_only_class,
)


class _DummyGateway(BaseGateway):
    """最小可实例化 BaseGateway —— 仅用于工厂方法套壳的测试基类。"""

    default_name: str = "DUMMY"
    exchanges: list = [Exchange.SHFE]

    def connect(self, setting: dict) -> None:  # pragma: no cover - 测试不调用
        pass

    def close(self) -> None:  # pragma: no cover
        pass

    def subscribe(self, req) -> None:  # pragma: no cover
        pass

    def send_order(self, req: OrderRequest) -> str:  # pragma: no cover
        return ""

    def cancel_order(self, req) -> None:  # pragma: no cover
        pass

    def query_account(self) -> None:  # pragma: no cover
        pass

    def query_position(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def event_engine():
    # 不调用 start() — 这样事件不会被 worker 线程异步取走，便于断言同步派发
    return EventEngine()


@pytest.fixture
def gateway(event_engine):
    cls = make_signal_only_class(_DummyGateway)
    gw = cls(event_engine, "DUMMY")
    gw.set_signal_notifier(NullNotifier())
    return gw


def _buy_req(price: float = 100.0, volume: int = 1, reference: str = "test_strat") -> OrderRequest:
    return OrderRequest(
        symbol="rb2510",
        exchange=Exchange.SHFE,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=volume,
        price=price,
        offset=Offset.OPEN,
        reference=reference,
    )


class TestSynchronousDispatch:
    """核心 bug 防回归：send_order 必须在返回前同步触发所有 EVENT_TRADE handler。"""

    def test_trade_handler_called_inside_send_order(self, event_engine, gateway):
        trade_events: list[Event] = []
        event_engine.register(EVENT_TRADE, trade_events.append)

        # 关键断言：send_order 调用结束的那一刻，trade_events 必须已被填充
        gateway.send_order(_buy_req())

        assert (
            len(trade_events) == 1
        ), "EVENT_TRADE 没在 send_order 内同步触发 — Gemini 指出的 self.pos 陷阱回归"
        assert trade_events[0].data.price == 100.0
        assert trade_events[0].data.volume == 1
        assert trade_events[0].data.direction == Direction.LONG

    def test_order_handler_called_inside_send_order(self, event_engine, gateway):
        order_events: list[Event] = []
        event_engine.register(EVENT_ORDER, order_events.append)

        gateway.send_order(_buy_req())

        assert len(order_events) == 1
        assert order_events[0].data.status == Status.ALLTRADED
        assert order_events[0].data.traded == 1

    def test_order_event_dispatched_before_trade_event(self, event_engine, gateway):
        """vnpy 实盘语义：ORDER 先于 TRADE 到达。SIGNAL_ONLY 必须保持同样顺序。"""
        order: list[tuple[str, object]] = []

        def order_handler(event):
            order.append(("ORDER", event.data.status))

        def trade_handler(event):
            order.append(("TRADE", event.data.price))

        event_engine.register(EVENT_ORDER, order_handler)
        event_engine.register(EVENT_TRADE, trade_handler)

        gateway.send_order(_buy_req())

        assert order == [("ORDER", Status.ALLTRADED), ("TRADE", 100.0)]

    def test_handler_exception_does_not_break_state_machine(self, event_engine, gateway):
        """单个下游 handler 异常不应阻断后续 handler 或 send_order 返回。"""
        crashed = []

        def bad_handler(event):
            crashed.append(1)
            raise RuntimeError("downstream blowup")

        good_calls: list[Event] = []
        event_engine.register(EVENT_TRADE, bad_handler)
        event_engine.register(EVENT_TRADE, good_calls.append)

        # 必须不抛出 — 否则策略 self.buy() 会从 on_tick 里冒泡上来
        vt_orderid = gateway.send_order(_buy_req())

        assert crashed == [1]
        assert len(good_calls) == 1
        assert vt_orderid.startswith("DUMMY.")


class TestPosLockoutAgainstTickStorm:
    """端到端模拟 Gemini 描述的场景：策略在 on_tick 里调 send_order，
    后续 tick 必须看到 self.pos 已经更新。"""

    def test_pos_locked_before_next_tick_processed(self, event_engine, gateway):
        # 用一个最小策略：on_tick 触发 send_order，trade handler 同步更新 pos
        class FakeStrategy:
            pos = 0
            send_count = 0

            def on_tick(self, _tick):
                if self.pos == 0:  # 模拟 "open if flat"
                    self.send_count += 1
                    gateway.send_order(_buy_req())

            def on_trade(self, event):
                self.pos += event.data.volume  # 简化版 CtaTemplate.on_trade

        strat = FakeStrategy()
        event_engine.register(EVENT_TRADE, strat.on_trade)

        # 模拟 3 个连续 tick，每个都让 on_tick 检查 pos
        for _ in range(3):
            strat.on_tick(None)

        # 若同步派发起作用：第一次 tick 入仓后 pos=1，后续两次 tick 跳过
        # 若退化为异步：3 次都满足 pos==0，触发 3 次 send_order
        assert strat.send_count == 1, (
            f"send_order 被触发 {strat.send_count} 次 — self.pos 没在 tick 间被锁定，"
            "回归到 Gemini 警告的开仓循环 bug"
        )
        assert strat.pos == 1


class TestOrderIdAndDetection:
    def test_orderid_has_signal_prefix(self, gateway):
        vt_orderid = gateway.send_order(_buy_req())
        _, orderid = vt_orderid.split(".", 1)
        assert orderid.startswith(SIGNAL_ORDERID_PREFIX)

    def test_orderid_is_monotonic(self, gateway):
        ids = [gateway.send_order(_buy_req()).split(".", 1)[1] for _ in range(3)]
        assert ids == sorted(ids)
        assert len(set(ids)) == 3

    def test_is_signal_trade_recognizes_synthesized(self, event_engine, gateway):
        captured: list = []
        event_engine.register(EVENT_TRADE, lambda e: captured.append(e.data))
        gateway.send_order(_buy_req())
        assert is_signal_trade(captured[0]) is True

    def test_is_signal_trade_rejects_real_trade(self):
        real_trade = MagicMock()
        real_trade.orderid = "1234567890"
        assert is_signal_trade(real_trade) is False

    def test_is_signal_trade_handles_missing_orderid(self):
        obj = MagicMock(spec=[])
        assert is_signal_trade(obj) is False


class TestCancelOrder:
    def test_cancel_is_noop(self, gateway):
        # 假成交瞬时完成，cancel 必须可调用且不报错
        req = MagicMock()
        req.orderid = "anything"
        gateway.cancel_order(req)  # 不应抛出


class TestNotifierIntegration:
    def test_signal_notification_sent_on_order(self, event_engine):
        cls = make_signal_only_class(_DummyGateway)
        gw = cls(event_engine, "DUMMY")
        notifier = MagicMock()
        gw.set_signal_notifier(notifier)

        gw.send_order(_buy_req(price=4500.0, volume=2, reference="strat_x"))

        assert notifier.send.call_count == 1
        kwargs = notifier.send.call_args.kwargs
        msg = notifier.send.call_args.args[0]
        assert "信号触发" in msg
        assert "strat_x" in msg
        assert "4500" in msg
        assert kwargs.get("force") is True  # 信号告警必须穿透 dedup/rate-limit

    def test_notifier_failure_does_not_break_state_dispatch(self, event_engine):
        """通知发送失败不能影响策略状态更新 — 状态机优先。"""
        cls = make_signal_only_class(_DummyGateway)
        gw = cls(event_engine, "DUMMY")
        notifier = MagicMock()
        notifier.send.side_effect = RuntimeError("network down")
        gw.set_signal_notifier(notifier)

        trade_seen: list = []
        event_engine.register(EVENT_TRADE, trade_seen.append)

        # 仍然必须成功返回
        vt_orderid = gw.send_order(_buy_req())

        assert vt_orderid.startswith("DUMMY.")
        assert len(trade_seen) == 1  # 状态先派发，notifier 后调用，顺序正确


class TestNotifyListenerSkipsSignalTrades:
    """NotifyListener 必须跳过 [SIGNAL] 合成成交，避免和 _notify_signal 重复推送。"""

    def test_signal_trade_skipped(self):
        from utils.notify_listener import NotifyListener

        notifier = MagicMock()
        ee = MagicMock()
        me = MagicMock()
        listener = NotifyListener(me, ee, notifier)
        notifier.reset_mock()  # 忽略 __init__ 里的"系统监控已启动"

        # 模拟合成 TradeData
        synth_trade = MagicMock()
        synth_trade.orderid = f"{SIGNAL_ORDERID_PREFIX}00000001"
        event = MagicMock(data=synth_trade)

        listener.on_trade(event)

        notifier.send_trade.assert_not_called()

    def test_real_trade_still_pushed(self):
        from utils.notify_listener import NotifyListener

        notifier = MagicMock()
        ee = MagicMock()
        me = MagicMock()
        listener = NotifyListener(me, ee, notifier)
        notifier.reset_mock()

        real = MagicMock()
        real.orderid = "1234567890"
        real.vt_symbol = "rb2510.SHFE"
        real.direction.value = "多"
        real.offset.value = "开"
        real.price = 4500.0
        real.volume = 1
        real.datetime = None
        real.reference = "real_strategy"
        event = MagicMock(data=real)

        listener.on_trade(event)

        notifier.send_trade.assert_called_once()
        args = notifier.send_trade.call_args.args
        assert args[0] == "real_strategy"

    def test_real_trade_without_reference_falls_back(self):
        """vnpy 某些版本 TradeData 没有 reference 字段，listener 不能因此 AttributeError。"""
        from utils.notify_listener import NotifyListener

        notifier = MagicMock()
        listener = NotifyListener(MagicMock(), MagicMock(), notifier)
        notifier.reset_mock()

        real = MagicMock(
            spec=["orderid", "vt_symbol", "direction", "offset", "price", "volume", "datetime"]
        )
        real.orderid = "1234567890"
        real.vt_symbol = "rb2510.SHFE"
        real.direction.value = "多"
        real.offset.value = "开"
        real.price = 4500.0
        real.volume = 1
        real.datetime = None
        # 注意：没有 reference 属性
        event = MagicMock(data=real)

        listener.on_trade(event)  # 不应抛 AttributeError

        notifier.send_trade.assert_called_once()
        assert notifier.send_trade.call_args.args[0] == "未知策略"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
