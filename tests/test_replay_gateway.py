"""ReplayGateway 单元/集成测试。

定位
----
ReplayGateway 是 REPLAY 模式的上游 — 把 DB bar 重放成 tick 流，下游沿用
SIGNAL_ONLY 的合成成交。所以本文件不重测下游合成（test_signal_only_gateway.py
覆盖了 20 个用例），而是聚焦：

1. ``send_order`` 复用合成 helper 后语义没退化（虚拟标记、同步派发、ALLTRADED）
2. ``start_replay`` 推 tick 的顺序、节拍、终态
3. RiskGuard 在风暴模式（delay_ms=0）下能被合成成交触发熔断
"""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, OrderType, Status
from vnpy.trader.event import EVENT_CONTRACT, EVENT_ORDER, EVENT_TICK, EVENT_TRADE
from vnpy.trader.object import BarData, OrderRequest

from tests._fakes import make_test_event_engine, stop_event_engine_fast
from utils.notifier import NullNotifier
from utils.replay_gateway import ReplayGateway
from utils.signal_only_gateway import SIGNAL_ORDERID_PREFIX, is_signal_trade


@pytest.fixture
def event_engine():
    """ReplayGateway 的 tick / contract 走 ``event_engine.put`` → 工作线程派发，
    所以这里必须 ``start()`` —— 与 SIGNAL_ONLY 测试用的同步路径不同。
    ``send_order`` 的合成成交仍是同步派发，不受影响。

    teardown 用 ``stop_event_engine_fast`` + ``ee.stop()``，避免每个 test 多花 ~1s
    在 worker/timer 线程 join 上（详见 _fakes.py）。
    """
    ee = make_test_event_engine()
    ee.start()
    yield ee
    stop_event_engine_fast(ee)
    ee.stop()


@pytest.fixture
def gateway(event_engine):
    gw = ReplayGateway(event_engine, "REPLAY")
    gw.set_signal_notifier(NullNotifier())
    return gw


def _drain(event_engine, timeout: float = 2.0) -> None:
    """忙等到 EventEngine._queue 排空。EventEngine 没暴露 wait_until_idle，只能 poll。"""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if event_engine._queue.empty():
            # 多睡一小会儿，给 worker 把最后一条事件交给 handler
            time.sleep(0.02)
            if event_engine._queue.empty():
                return
        time.sleep(0.01)
    raise TimeoutError(f"event queue 在 {timeout}s 内未排空")


def _bar(close: float, dt: datetime | None = None) -> BarData:
    return BarData(
        gateway_name="REPLAY",
        symbol="rb2410",
        exchange=Exchange.SHFE,
        datetime=dt or datetime(2024, 1, 1, 10, 0, 0),
        interval=Interval.HOUR,
        open_price=close,
        high_price=close,
        low_price=close,
        close_price=close,
        volume=1,
        turnover=close,
        open_interest=0,
    )


def _buy_req(price: float = 4500.0, reference: str = "test") -> OrderRequest:
    return OrderRequest(
        symbol="rb2410",
        exchange=Exchange.SHFE,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=1,
        price=price,
        offset=Offset.OPEN,
        reference=reference,
    )


class TestSendOrderSynthesis:
    """复用 signal_only_gateway 顶层 helper 后，REPLAY 同步派发语义没退化。"""

    def test_order_and_trade_dispatched_synchronously(self, event_engine, gateway):
        orders: list = []
        trades: list = []
        event_engine.register(EVENT_ORDER, lambda e: orders.append(e.data))
        event_engine.register(EVENT_TRADE, lambda e: trades.append(e.data))

        gateway.send_order(_buy_req(price=4500))

        # 同步派发：send_order 返回时事件必须已经触发
        assert len(orders) == 1
        assert len(trades) == 1
        assert orders[0].status == Status.ALLTRADED

    def test_synthesized_objects_marked_virtual(self, event_engine, gateway):
        trades: list = []
        event_engine.register(EVENT_TRADE, lambda e: trades.append(e.data))
        gateway.send_order(_buy_req())
        assert is_signal_trade(trades[0])
        assert trades[0].is_virtual is True
        assert trades[0].orderid.startswith(SIGNAL_ORDERID_PREFIX)

    def test_vt_orderid_carries_gateway_name(self, gateway):
        vt = gateway.send_order(_buy_req())
        assert vt.startswith("REPLAY.")


class TestConnectPushesContract:
    """CtaEngine.subscribe_data 依赖 main_engine.contracts 里有对应合约。"""

    def test_connect_pushes_contract_data(self, event_engine, gateway):
        received: list = []
        event_engine.register(EVENT_CONTRACT, lambda e: received.append(e.data))

        gateway.connect({"symbols": [("rb2410", Exchange.SHFE)]})
        _drain(event_engine)

        assert len(received) == 1
        contract = received[0]
        assert contract.symbol == "rb2410"
        assert contract.exchange == Exchange.SHFE
        assert contract.vt_symbol == "rb2410.SHFE"


class TestReplayTickStream:
    """逐 bar emit tick + 末尾 flush tick；顺序、价格、minute 错位都对齐 BarGenerator 预期。"""

    def test_emits_one_tick_per_bar_plus_flush(self, event_engine, gateway):
        ticks: list = []
        event_engine.register(EVENT_TICK, lambda e: ticks.append(e.data))

        bars = [_bar(100 + i) for i in range(5)]
        gateway.start_replay(bars, delay_ms=0, block=True)
        _drain(event_engine)

        # 5 bar → 5 主 tick + 1 flush tick
        assert len(ticks) == 6
        # 顺序：close_price 按输入序列递增
        assert [t.last_price for t in ticks[:5]] == [100, 101, 102, 103, 104]
        # 末尾 flush tick 沿用最后一根 bar 的 close
        assert ticks[5].last_price == 104

    def test_tick_minutes_strictly_monotonic(self, event_engine, gateway):
        """BarGenerator 用 datetime.minute 切窗，相邻 tick 必须分钟错开。"""
        ticks: list = []
        event_engine.register(EVENT_TICK, lambda e: ticks.append(e.data))

        gateway.start_replay([_bar(100), _bar(101), _bar(102)], delay_ms=0, block=True)
        _drain(event_engine)

        minutes = [(t.datetime.hour, t.datetime.minute) for t in ticks]
        assert len(set(minutes)) == len(minutes), f"tick datetime 重复：{minutes}"
        assert minutes == sorted(minutes)

    def test_empty_bars_does_not_crash(self, event_engine, gateway):
        ticks: list = []
        event_engine.register(EVENT_TICK, lambda e: ticks.append(e.data))
        gateway.start_replay([], delay_ms=0, block=True)
        _drain(event_engine)
        assert ticks == []

    def test_delay_ms_honored(self, event_engine, gateway):
        """delay_ms=50ms × 4 bar ≈ 200ms。给较宽容差防 CI 抖动。"""
        event_engine.register(EVENT_TICK, lambda e: None)
        bars = [_bar(100 + i) for i in range(4)]

        t0 = time.perf_counter()
        gateway.start_replay(bars, delay_ms=50, block=True)
        elapsed = time.perf_counter() - t0

        # 下限：3 个 sleep(50ms) 之后才进入第 4 个 bar 的 tick（最后一根不再 sleep
        # 也就是大约 ≥ 150ms；这里给 ≥ 100ms 留余量
        assert 0.10 <= elapsed <= 1.5, f"实际耗时 {elapsed:.3f}s，与节拍偏差太大"


class TestStopReplay:
    def test_stop_flag_aborts_loop(self, event_engine, gateway):
        ticks: list = []
        event_engine.register(EVENT_TICK, lambda e: ticks.append(e.data))

        # 中等数量 bar + 较慢节拍 — 让 main 线程能在中途 stop
        bars = [_bar(100 + i) for i in range(50)]
        thread = gateway.start_replay(bars, delay_ms=20, block=False)
        time.sleep(0.10)  # 至少跑过几根
        gateway.stop_replay()
        thread.join(timeout=2.0)
        _drain(event_engine)

        assert not thread.is_alive()
        assert 1 <= len(ticks) < 50, f"stop_replay 后 tick 数 {len(ticks)} 异常"


class TestLogicalTimeIsolation:
    """Gemini 2026-05-19 拷问：REPLAY 把数月数据压成 1.7 分钟物理时间，如果合成
    trade.datetime 用 datetime.now()，RiskGuard 的 60s 窗口会立刻假性熔断。

    修正：ReplayGateway._current_synthetic_dt 在 _replay_loop 里追逐 tick 的逻辑时间，
    send_order 用它写 trade.datetime。"""

    def test_trade_datetime_tracks_synthetic_dt_not_wallclock(self, event_engine, gateway):
        """on_bar 同步触发 send_order 时，合成的 trade.datetime 必须 == 当前 tick 的
        synthetic_dt，而不是 datetime.now()。"""
        captured_trades: list = []
        event_engine.register(EVENT_TRADE, lambda e: captured_trades.append(e.data))

        # 模拟 on_bar 回调链：tick → BarGenerator → on_bar → send_order
        # 这里直接在 EVENT_TICK handler 内调 send_order，模拟"同步报单"
        def _tick_to_order(event):
            gateway.send_order(_buy_req())

        event_engine.register(EVENT_TICK, _tick_to_order)

        bars = [_bar(100 + i) for i in range(5)]
        gateway.start_replay(bars, delay_ms=0, block=True)
        _drain(event_engine)

        assert len(captured_trades) >= 5
        # 每个 trade.datetime 必须落在 base_dt..base_dt+10min 这个逻辑窗口内，
        # 绝不是 wall-clock now（now 此刻一般是 2026 年）
        for trade in captured_trades:
            assert trade.datetime.year == 2026, "trade.datetime 不在合成窗口内"
            assert trade.datetime.month == 1
            assert trade.datetime.day == 1
            assert trade.datetime.hour == 9

    def test_replay_does_not_trip_riskguard_on_logical_time(self, event_engine):
        """50 根 bar × 每根 1 笔合成成交 = 50 个 trade，但逻辑时间跨 50 分钟 →
        RiskGuard.max_trades_per_minute=20 不应触发。这是 wall-clock vs logical-time
        修复后的核心保障。"""
        from utils.risk_guard import RiskGuard

        gw = ReplayGateway(event_engine, "REPLAY")
        gw.set_signal_notifier(NullNotifier())

        guard = RiskGuard(
            main_engine=MagicMock(),
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_daily_loss_pct=1.0,
            max_position_per_symbol=10_000,
            max_trades_per_minute=20,
            breach_flag_path=str(__import__("tempfile").mkstemp(suffix=".flag")[1]),
            startup_sync_timeout_s=None,
        )

        # 模拟"每根 bar 触一笔成交"
        event_engine.register(EVENT_TICK, lambda e: gw.send_order(_buy_req()))

        bars = [_bar(100 + i) for i in range(50)]
        gw.start_replay(bars, delay_ms=0, block=True)
        _drain(event_engine)

        assert (
            guard.tripped is False
        ), f"RiskGuard 在 50-bar REPLAY 中假性熔断：{guard.trip_reason} — wall-clock 污染回归"
        # trade_window 里仍有 trade，但都落在不同的逻辑分钟，60s 窗口内 ≤1 个
        assert len(guard.trade_window) >= 1


class TestRiskGuardStormStillTripsOutsideReplay:
    """非回放路径（直接 send_order）仍以 wall-clock 为准，限频阈值机制本身没退化。"""

    def test_rate_limit_breach_trips_guard_via_wallclock_fallback(self, event_engine):
        from utils.risk_guard import RiskGuard

        gw = ReplayGateway(event_engine, "REPLAY")
        gw.set_signal_notifier(NullNotifier())
        # 没起 start_replay → _current_synthetic_dt 仍为 None → send_order 回退
        # 到 datetime.now()。这条路径用于验证 RiskGuard 限频机制本身没坏。

        guard = RiskGuard(
            main_engine=MagicMock(),
            event_engine=event_engine,
            notifier=NullNotifier(),
            max_daily_loss_pct=1.0,
            max_position_per_symbol=10_000,
            max_trades_per_minute=20,
            breach_flag_path=str(__import__("tempfile").mkstemp(suffix=".flag")[1]),
            startup_sync_timeout_s=None,
        )

        for _ in range(21):
            gw.send_order(_buy_req())

        assert guard.tripped is True
        assert "60 秒成交" in guard.trip_reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
