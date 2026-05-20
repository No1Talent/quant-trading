"""RiskGuard 单元测试：日内回撤 / 持仓 / 成交频次 / 熔断标志 / pre-gate / 并发。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests._fakes import make_position
from utils.notifier import NullNotifier
from utils.risk_guard import RiskGuard, check_breach_flag


@pytest.fixture
def event_engine():
    ee = MagicMock()
    ee.register = MagicMock()
    ee.unregister = MagicMock()
    return ee


@pytest.fixture
def main_engine():
    me = MagicMock()
    me.cancel_all_active_orders = MagicMock()
    return me


@pytest.fixture
def tmp_flag(tmp_path):
    return tmp_path / "risk_breach.flag"


@pytest.fixture
def guard(main_engine, event_engine, tmp_flag):
    # startup_sync_timeout_s=None: 默认禁用 fallback timer，单独的 TestStartupSyncFallback
    # 会显式开启或手动触发。否则每个测试都会留下一个后台 Timer 线程。
    return RiskGuard(
        main_engine=main_engine,
        event_engine=event_engine,
        notifier=NullNotifier(),
        max_daily_loss_pct=0.05,
        max_position_per_symbol=3,
        max_trades_per_minute=5,
        max_price_deviation=0.05,
        max_tick_age_seconds=60.0,
        breach_flag_path=tmp_flag,
        startup_sync_timeout_s=None,
    )


def _tick_event(vt_symbol: str, last_price: float, ts: datetime | None = None):
    return SimpleNamespace(
        data=SimpleNamespace(
            vt_symbol=vt_symbol,
            last_price=last_price,
            datetime=ts or datetime.now(),
        )
    )


def _account_event(balance: float):
    return SimpleNamespace(data=SimpleNamespace(balance=balance, accountid="TEST"))


def _trade_event(
    vt_symbol: str, direction: str, offset: str, volume: int, ts: datetime | None = None
):
    return SimpleNamespace(
        data=SimpleNamespace(
            vt_symbol=vt_symbol,
            direction=SimpleNamespace(value=direction),
            offset=SimpleNamespace(value=offset),
            volume=volume,
            datetime=ts or datetime.now(),
        )
    )


class TestDailyLoss:
    def test_daily_baseline_locked_on_first_event(self, guard):
        guard.on_account(_account_event(100_000))
        assert guard.daily_start_balance == 100_000
        assert guard.tripped is False

    def test_loss_below_threshold_not_tripped(self, guard):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(96_000))  # -4% < 5%
        assert guard.tripped is False

    def test_loss_above_threshold_trips(self, guard, main_engine):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(94_000))  # -6%
        assert guard.tripped is True
        main_engine.cancel_all_active_orders.assert_called_once()

    def test_zero_baseline_does_not_div_by_zero(self, guard):
        guard.on_account(_account_event(0))
        guard.on_account(_account_event(0))
        assert guard.tripped is False


class TestPositionLimit:
    def test_position_under_limit_not_tripped(self, guard):
        for _ in range(3):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
        assert guard.tripped is False
        assert guard.position["rb2510.SHFE"] == 3

    def test_position_over_limit_trips(self, guard, main_engine):
        for _ in range(4):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
        assert guard.tripped is True
        main_engine.cancel_all_active_orders.assert_called_once()

    def test_close_reduces_position(self, guard):
        for _ in range(3):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
        guard.on_trade(_trade_event("rb2510.SHFE", "空", "平", 2))
        assert guard.position["rb2510.SHFE"] == 1


class TestTradeFrequency:
    def test_under_freq_not_tripped(self, guard):
        # 避开持仓上限：交替开平，净持仓最高 1
        for _ in range(2):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
            guard.on_trade(_trade_event("rb2510.SHFE", "空", "平", 1))
        # 4 笔 < 阈值 5
        assert guard.tripped is False

    def test_over_freq_trips(self, guard, main_engine):
        for _ in range(3):
            guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
            guard.on_trade(_trade_event("rb2510.SHFE", "空", "平", 1))
        # 6 笔 > 阈值 5
        assert guard.tripped is True


class TestTrippedBehavior:
    def test_cancel_called_on_trip(self, guard, main_engine):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))
        main_engine.cancel_all_active_orders.assert_called_once()

    def test_subsequent_events_dont_recancel(self, guard, main_engine):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))
        guard.on_account(_account_event(85_000))
        guard.on_trade(_trade_event("rb2510.SHFE", "多", "开", 10))
        assert main_engine.cancel_all_active_orders.call_count == 1

    def test_cancel_fallback_to_legacy_api(self, event_engine, tmp_flag):
        legacy = MagicMock(spec=["cancel_all_orders", "get_all_positions"])
        legacy.cancel_all_orders = MagicMock()
        legacy.get_all_positions = MagicMock(return_value=[])
        g = RiskGuard(
            main_engine=legacy,
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_daily_loss_pct=0.05,
            max_position_per_symbol=3,
            max_trades_per_minute=5,
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        g.on_account(_account_event(100_000))
        g.on_account(_account_event(90_000))
        legacy.cancel_all_orders.assert_called_once()


class TestBreachFlag:
    def test_flag_written_on_trip(self, guard, tmp_flag):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))
        assert tmp_flag.exists()

        loaded = check_breach_flag(tmp_flag)
        assert loaded is not None
        assert "tripped_at" in loaded
        assert "日内亏损" in loaded["reason"]

    def test_check_returns_none_when_no_flag(self, tmp_path):
        assert check_breach_flag(tmp_path / "nope.flag") is None

    def test_reset_removes_flag(self, guard, tmp_flag):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))
        assert tmp_flag.exists()
        guard.reset()
        assert not tmp_flag.exists()
        assert guard.tripped is False


class TestUnderlyingPositionLimit:
    """新增：max_position_per_underlying 把"单合约限额"提升为"标的级汇总限额"，
    防止同标的多个合约月份累加绕过 max_position_per_symbol。"""

    @pytest.fixture
    def reg(self):
        # 用真实默认 YAML：保证 rb2410 / rb2501 都解析为 RB
        from utils.product_registry import ProductRegistry, set_default_registry

        r = ProductRegistry.load()
        set_default_registry(r)
        yield r
        set_default_registry(None)

    @pytest.fixture
    def underlying_guard(self, main_engine, event_engine, tmp_flag, reg):
        return RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_daily_loss_pct=0.05,
            max_position_per_symbol=5,
            max_position_per_underlying=6,
            max_trades_per_minute=50,
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )

    def test_underlying_aggregates_two_months_of_same_product(self, underlying_guard):
        # rb2410 +3, rb2501 +3 → 单合约都没超 5，但标的汇总 +6 == 阈值 不熔
        for _ in range(3):
            underlying_guard.on_trade(_trade_event("rb2410.SHFE", "多", "开", 1))
        for _ in range(3):
            underlying_guard.on_trade(_trade_event("rb2501.SHFE", "多", "开", 1))
        assert underlying_guard.tripped is False
        assert underlying_guard.underlying_position["RB"] == 6

    def test_underlying_aggregate_over_threshold_trips(self, underlying_guard, main_engine):
        for _ in range(4):
            underlying_guard.on_trade(_trade_event("rb2410.SHFE", "多", "开", 1))
        for _ in range(3):  # 累计 RB = 7 > 6
            underlying_guard.on_trade(_trade_event("rb2501.SHFE", "多", "开", 1))
        assert underlying_guard.tripped is True
        assert "标的 RB" in underlying_guard.trip_reason
        main_engine.cancel_all_active_orders.assert_called_once()

    def test_opposite_directions_net_out_in_underlying(self, underlying_guard):
        # rb2410 多 4, rb2501 空 4 → 标的净持仓为 0，不熔
        for _ in range(4):
            underlying_guard.on_trade(_trade_event("rb2410.SHFE", "多", "开", 1))
        for _ in range(4):
            underlying_guard.on_trade(_trade_event("rb2501.SHFE", "空", "开", 1))
        assert underlying_guard.tripped is False
        assert underlying_guard.underlying_position["RB"] == 0

    def test_unregistered_symbol_skips_underlying_check(self, underlying_guard):
        # zzz9999 在 ProductRegistry 里没注册 — 标的级检查必须自动跳过，
        # 不能因为没在 YAML 配就把风控引擎抛崩
        for _ in range(5):  # 还卡在 max_position_per_symbol=5
            underlying_guard.on_trade(_trade_event("zzz9999.SHFE", "多", "开", 1))
        assert underlying_guard.tripped is False
        # zzz9999 不应该出现在 underlying_position 里
        assert "ZZZ" not in underlying_guard.underlying_position

    def test_default_off_when_param_omitted(self, main_engine, event_engine, tmp_flag, reg):
        # 不传 max_position_per_underlying → 完全保持向后兼容，
        # 同标的两个月份相加超总额也只受 per_symbol 约束
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_position_per_symbol=5,
            max_trades_per_minute=50,
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        for _ in range(3):
            g.on_trade(_trade_event("rb2410.SHFE", "多", "开", 1))
        for _ in range(3):
            g.on_trade(_trade_event("rb2501.SHFE", "多", "开", 1))
        # 标的 RB 合计 6 但 per-underlying 关着，不熔
        assert g.tripped is False
        # underlying_position 仍然被维护（便于 dashboards），只是不参与规则
        # 注：未设 product_registry 时 underlying 解析返回 None，故未聚合
        assert g.underlying_position == {}

    def test_sync_from_engine_populates_underlying_aggregate(
        self, main_engine, event_engine, tmp_flag, reg
    ):
        # 启动期 OmsEngine 已经有持仓 — 标的汇总必须在 sync 时一并重算
        main_engine.get_all_positions = lambda: [
            make_position("rb2410.SHFE", "多", 2),
            make_position("rb2501.SHFE", "多", 3),
            make_position("ag2506.SHFE", "空", 1),
        ]
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_position_per_symbol=10,
            max_position_per_underlying=8,
            max_trades_per_minute=50,
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        g._sync_positions_from_engine()
        assert g.underlying_position["RB"] == 5
        assert g.underlying_position["AG"] == -1


class TestConcurrency:
    def test_concurrent_trade_events_dont_crash(self, guard):
        errors: list[str] = []

        def worker(i: int):
            try:
                for _ in range(20):
                    guard.on_trade(_trade_event(f"sym{i}.SHFE", "多", "开", 1))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestRegister:
    def test_register_called_on_init(self, event_engine, main_engine, tmp_flag):
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        # EVENT_TRADE + EVENT_ACCOUNT + EVENT_TICK
        assert event_engine.register.call_count == 3
        g.unregister()

    def test_unregister(self, guard, event_engine):
        guard.unregister()
        assert event_engine.unregister.call_count == 3


class TestPreGate:
    def test_tick_cache_updated_on_event(self, guard):
        guard.on_tick(_tick_event("rb2510.SHFE", 4000.0))
        assert guard.latest_tick_price["rb2510.SHFE"] == 4000.0

    def test_dirty_zero_tick_not_cached(self, guard):
        guard.on_tick(_tick_event("rb2510.SHFE", 0.0))
        assert "rb2510.SHFE" not in guard.latest_tick_price

    def test_allow_within_deviation(self, guard):
        guard.on_tick(_tick_event("rb2510.SHFE", 4000.0))
        allowed, reason = guard.check_order_pre("rb2510.SHFE", "long", 4100.0)
        assert allowed is True
        assert reason == "ok"
        assert guard.pre_gate_rejects == 0

    def test_reject_over_deviation(self, guard):
        guard.on_tick(_tick_event("rb2510.SHFE", 4000.0))
        # +6% > 5% threshold
        allowed, reason = guard.check_order_pre("rb2510.SHFE", "long", 4240.0)
        assert allowed is False
        assert reason == "price_deviation_exceeded"
        assert guard.pre_gate_rejects == 1

    def test_reject_dirty_zero_price(self, guard):
        guard.on_tick(_tick_event("rb2510.SHFE", 4000.0))
        allowed, reason = guard.check_order_pre("rb2510.SHFE", "long", 0.0)
        assert allowed is False
        assert reason == "non_positive_price"

    def test_reject_negative_price(self, guard):
        guard.on_tick(_tick_event("rb2510.SHFE", 4000.0))
        allowed, reason = guard.check_order_pre("rb2510.SHFE", "long", -1.0)
        assert allowed is False
        assert reason == "non_positive_price"

    def test_reject_when_no_reference(self, guard):
        allowed, reason = guard.check_order_pre("rb2510.SHFE", "long", 4000.0)
        assert allowed is False
        assert reason == "no_reference_price"

    def test_reject_when_tripped(self, guard, main_engine):
        guard.on_account(_account_event(100_000))
        guard.on_account(_account_event(90_000))  # 触发熔断
        assert guard.tripped is True
        guard.on_tick(_tick_event("rb2510.SHFE", 4000.0))
        allowed, reason = guard.check_order_pre("rb2510.SHFE", "long", 4000.0)
        assert allowed is False
        assert reason == "tripped"

    def test_reject_stale_tick(self, guard):
        old_ts = datetime.now() - timedelta(seconds=120)
        guard.on_tick(_tick_event("rb2510.SHFE", 4000.0, ts=old_ts))
        allowed, reason = guard.check_order_pre("rb2510.SHFE", "long", 4000.0)
        assert allowed is False
        assert reason == "stale_reference_price"

    def test_explicit_reference_bypasses_cache(self, guard):
        # 缓存里没行情，但调用方明确传 reference_price 也允许（双签覆盖场景）
        allowed, reason = guard.check_order_pre(
            "rb2510.SHFE", "long", 4100.0, reference_price=4000.0
        )
        assert allowed is True
        assert reason == "ok"

    def test_explicit_reference_still_checks_deviation(self, guard):
        allowed, reason = guard.check_order_pre(
            "rb2510.SHFE", "long", 4300.0, reference_price=4000.0
        )
        assert allowed is False
        assert reason == "price_deviation_exceeded"

    def test_pre_gate_does_not_trip_guard(self, guard):
        """单笔被 pre-gate 拦截属正常防御，不应升级到全账户熔断。"""
        guard.on_tick(_tick_event("rb2510.SHFE", 4000.0))
        for _ in range(10):
            guard.check_order_pre("rb2510.SHFE", "buy", 8000.0)  # 100% 偏离
        assert guard.tripped is False
        assert guard.pre_gate_rejects == 10

    def test_reject_alerts_are_throttled_per_symbol_reason(
        self, main_engine, event_engine, tmp_flag
    ):
        """同 (合约, 原因) 在冷却窗口内只 push 一次告警，避免告警风暴。"""
        notifier = MagicMock()
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=notifier,
            max_daily_loss_pct=0.05,
            max_position_per_symbol=3,
            max_trades_per_minute=5,
            max_price_deviation=0.05,
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        notifier.send.reset_mock()  # 忽略构造时的"启动"通知

        g.on_tick(_tick_event("rb2510.SHFE", 4000.0))
        for _ in range(20):
            g.check_order_pre("rb2510.SHFE", "buy", 8000.0)
        assert g.pre_gate_rejects == 20
        # 第一次拦截发告警，后续 19 次被节流
        assert notifier.send.call_count == 1

        # 不同 reason 不共享节流桶
        g.check_order_pre("rb2510.SHFE", "buy", 0.0)  # non_positive_price
        assert notifier.send.call_count == 2


class TestStartupSyncFallback:
    """启动 fallback：若 EVENT_ACCOUNT 没及时到达，主动从引擎同步持仓基线。

    不依赖真实 Timer——用 startup_sync_timeout_s=None 关掉自动调度，手工触发
    `_run_startup_sync_fallback()` 模拟 timer 到期，避免后台线程污染测试。
    """

    def test_fallback_loads_positions_when_account_event_late(
        self, main_engine, event_engine, tmp_flag
    ):
        main_engine.get_all_positions = MagicMock(
            return_value=[
                make_position("rb2510.SHFE", "多", 2),
                make_position("ag2512.SHFE", "空", 1),
            ]
        )
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_position_per_symbol=3,
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        assert g.position == {}  # 启动后空
        g._run_startup_sync_fallback()
        assert g.position["rb2510.SHFE"] == 2
        assert g.position["ag2512.SHFE"] == -1
        assert g._initial_sync_done is True

    def test_fallback_skipped_when_account_already_arrived(
        self, main_engine, event_engine, tmp_flag
    ):
        main_engine.get_all_positions = MagicMock(
            return_value=[make_position("rb2510.SHFE", "多", 2)]
        )
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        g.on_account(_account_event(100_000))
        assert g._initial_sync_done is True
        main_engine.get_all_positions.reset_mock()
        g._run_startup_sync_fallback()
        # 已同步过的不应再读引擎
        main_engine.get_all_positions.assert_not_called()

    def test_fallback_sends_warning_notification(self, main_engine, event_engine, tmp_flag):
        main_engine.get_all_positions = MagicMock(return_value=[])
        notifier = MagicMock()
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=notifier,
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        notifier.send.reset_mock()
        g._run_startup_sync_fallback()
        notifier.send.assert_called_once()
        msg = notifier.send.call_args.args[0]
        assert "未收到账户事件" in msg

    def test_position_check_uses_synced_baseline(self, main_engine, event_engine, tmp_flag):
        """fallback 后再发交易事件，规则校验应该叠加到已同步的基线上，而不是从 0 起。"""
        main_engine.get_all_positions = MagicMock(
            return_value=[make_position("rb2510.SHFE", "多", 3)]
        )
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_position_per_symbol=3,
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        g._run_startup_sync_fallback()
        assert g.position["rb2510.SHFE"] == 3
        # 再开 1 手 → 持仓 4 > 阈值 3 → 应熔断；若没 fallback 则规则会从 0+1=1 起算，假阴性
        g.on_trade(_trade_event("rb2510.SHFE", "多", "开", 1))
        assert g.tripped is True

    def test_timer_is_scheduled_when_timeout_positive(self, main_engine, event_engine, tmp_flag):
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=60.0,  # 长延时避免测试期间真的触发
        )
        assert g._startup_timer is not None
        assert g._startup_timer.is_alive()
        g.unregister()
        # cancel 后线程标记终止，is_alive 可能仍 True 但已不会触发
        assert g._startup_timer is None

    def test_timer_not_scheduled_when_timeout_none(self, main_engine, event_engine, tmp_flag):
        g = RiskGuard(
            main_engine=main_engine,
            event_engine=event_engine,
            notifier=NullNotifier(),
            breach_flag_path=tmp_flag,
            startup_sync_timeout_s=None,
        )
        assert g._startup_timer is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
