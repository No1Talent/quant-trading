"""NotifyListener: keyword escalation, recursion guard, order rejection, account, strategy status."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from vnpy.trader.constant import Status

from utils.notifier import NotifyLevel, NullNotifier
from utils.notify_listener import NotifyListener


@pytest.fixture
def event_engine():
    ee = MagicMock()
    ee.register = MagicMock()
    ee.unregister = MagicMock()
    return ee


@pytest.fixture
def notifier():
    return MagicMock(spec=NullNotifier)


@pytest.fixture
def listener(event_engine, notifier):
    return NotifyListener(
        main_engine=MagicMock(),
        event_engine=event_engine,
        notifier=notifier,
    )


def _log_event(msg: str, gateway_name: str = "") -> SimpleNamespace:
    return SimpleNamespace(data=SimpleNamespace(msg=msg, gateway_name=gateway_name))


def _order_event(status: Status) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            status=status,
            vt_symbol="rb2510.SHFE",
            direction=SimpleNamespace(value="多"),
            offset=SimpleNamespace(value="开"),
            price=100.0,
            volume=1,
            reference="test_strategy",
        )
    )


def _account_event(balance: float, available: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(balance=balance, available=available, accountid="TEST")
    )


def _strategy_event(name: str, inited: bool, trading: bool) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            "strategy_name": name,
            "inited": inited,
            "trading": trading,
            "vt_symbol": "rb2510.SHFE",
            "pos": 0,
        }
    )


def _trade_event() -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            reference="test_strategy",
            vt_symbol="rb2510.SHFE",
            direction=SimpleNamespace(value="多"),
            offset=SimpleNamespace(value="开"),
            price=100.0,
            volume=1,
            datetime=datetime.now(),
        )
    )


class TestRegistration:
    def test_six_events_registered_on_init(self, event_engine, notifier):
        NotifyListener(MagicMock(), event_engine, notifier)
        assert event_engine.register.call_count == 6

    def test_unregister_removes_all(self, listener, event_engine):
        listener.unregister()
        assert event_engine.unregister.call_count == 6


class TestLogKeywords:
    def test_critical_keyword_triggers_send_critical(self, listener, notifier):
        notifier.reset_mock()
        listener.on_log(_log_event("CTP:行情前置不活跃"))
        notifier.send_critical.assert_called_once()

    def test_warning_keyword_triggers_send_at_warning_level(self, listener, notifier):
        notifier.reset_mock()
        listener.on_log(_log_event("下单失败: 价格超限"))
        notifier.send.assert_called_once()
        assert notifier.send.call_args.kwargs.get("level") == NotifyLevel.WARNING

    def test_own_log_skipped_to_prevent_recursion(self, listener, notifier):
        notifier.reset_mock()
        listener.on_log(_log_event("[Notifier] delivery failed"))
        notifier.send_critical.assert_not_called()
        notifier.send.assert_not_called()

    def test_gateway_name_self_skipped(self, listener, notifier):
        notifier.reset_mock()
        listener.on_log(_log_event("连接失败", gateway_name="Notifier"))
        notifier.send_critical.assert_not_called()
        notifier.send.assert_not_called()

    def test_critical_takes_priority_over_warning_keyword(self, listener, notifier):
        # "断线" is CRITICAL, but the message also contains "失败" (WARNING keyword)
        notifier.reset_mock()
        listener.on_log(_log_event("断线: 连接失败"))
        notifier.send_critical.assert_called_once()
        notifier.send.assert_not_called()

    def test_plain_log_not_forwarded(self, listener, notifier):
        notifier.reset_mock()
        listener.on_log(_log_event("策略初始化完成"))
        notifier.send_critical.assert_not_called()
        # send may have been called only if a WARNING keyword matched — none should here
        for call in notifier.send.call_args_list:
            assert call.kwargs.get("level") != NotifyLevel.WARNING


class TestRecoveryOverride:
    """关键词为子串匹配，'断开/失败' 会误命中恢复消息。RECOVERY_OVERRIDE 应当压住假阳。"""

    @pytest.mark.parametrize(
        "msg",
        [
            "CTP:行情前置不活跃，已重连",
            "网络异常，重连成功",
            "断开后已恢复",
            "下单失败重试成功",
            "Connection refused, reconnected",
            "Login failed and recovered",
        ],
    )
    def test_recovery_message_suppressed(self, listener, notifier, msg):
        notifier.reset_mock()
        listener.on_log(_log_event(msg))
        notifier.send_critical.assert_not_called()
        notifier.send.assert_not_called()

    def test_genuine_failure_still_fires(self, listener, notifier):
        """未含恢复标记的真失败必须照常告警，不能被白名单一刀切。"""
        notifier.reset_mock()
        listener.on_log(_log_event("下单失败: 资金不足"))
        notifier.send.assert_called_once()
        assert notifier.send.call_args.kwargs.get("level") == NotifyLevel.WARNING

    def test_genuine_disconnect_still_critical(self, listener, notifier):
        notifier.reset_mock()
        listener.on_log(_log_event("CTP:交易前置不活跃"))
        notifier.send_critical.assert_called_once()


class TestOrderRejection:
    def test_rejected_order_sends_warning(self, listener, notifier):
        notifier.reset_mock()
        listener.on_order(_order_event(Status.REJECTED))
        notifier.send.assert_called_once()

    def test_non_rejected_order_silent(self, listener, notifier):
        notifier.reset_mock()
        listener.on_order(_order_event(Status.NOTTRADED))
        notifier.send.assert_not_called()


class TestTrade:
    def test_trade_event_forwarded_to_send_trade(self, listener, notifier):
        notifier.reset_mock()
        listener.on_trade(_trade_event())
        notifier.send_trade.assert_called_once()


class TestAccountMonitor:
    def test_first_event_sets_baseline_and_notifies(self, listener, notifier):
        notifier.reset_mock()
        listener.on_account(_account_event(100_000))
        assert listener.last_balance == 100_000
        notifier.send.assert_called_once()

    def test_small_change_below_threshold_silent(self, listener, notifier):
        listener.on_account(_account_event(100_000))
        notifier.reset_mock()
        listener.on_account(_account_event(102_000))  # +2% < 5%
        notifier.send.assert_not_called()

    def test_large_change_above_threshold_warns(self, listener, notifier):
        listener.on_account(_account_event(100_000))
        notifier.reset_mock()
        listener.on_account(_account_event(90_000))  # -10% > 5%
        notifier.send.assert_called_once()


class TestStrategyStatus:
    def test_start_transition_sends_notification(self, listener, notifier):
        notifier.reset_mock()
        listener.on_cta_strategy(_strategy_event("S1", inited=True, trading=False))
        listener.on_cta_strategy(_strategy_event("S1", inited=True, trading=True))
        assert notifier.send.call_count == 1
        assert "启动" in notifier.send.call_args.args[0]

    def test_stop_transition_sends_notification(self, listener, notifier):
        listener.on_cta_strategy(_strategy_event("S1", inited=True, trading=True))
        notifier.reset_mock()
        listener.on_cta_strategy(_strategy_event("S1", inited=True, trading=False))
        notifier.send.assert_called_once()

    def test_same_status_no_notification(self, listener, notifier):
        listener.on_cta_strategy(_strategy_event("S1", inited=True, trading=True))
        notifier.reset_mock()
        listener.on_cta_strategy(_strategy_event("S1", inited=True, trading=True))
        notifier.send.assert_not_called()

    def test_non_dict_data_ignored(self, listener, notifier):
        notifier.reset_mock()
        listener.on_cta_strategy(SimpleNamespace(data="not a dict"))
        notifier.send.assert_not_called()
