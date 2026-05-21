"""测试装置：FakeClock、vn.py 事件工厂、EventEngine 快速停机助手。

FakeClock 是 reconciler 测试的核心 — 把时间从全局副作用变为显式依赖，让单线程的
sleep() 调用同时承担"推进虚拟时间"与"按时序触发预定回调"两个职责。
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vnpy.event import EventEngine


class FakeClock:
    """虚拟时钟。sleep() 既推进时间也触发到期回调，单线程同步语义。"""

    def __init__(self) -> None:
        self._t = 0.0
        # 每项 (fire_at_virtual_time, callback)。允许重复时间，按插入顺序触发。
        self._schedule: list[tuple[float, Callable[[], None]]] = []

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        """推进虚拟时间 seconds 秒，期间到期的预定回调按时序触发。"""
        if seconds < 0:
            raise ValueError("sleep seconds must be non-negative")
        target = self._t + seconds
        # 按 fire_at 升序取出该窗口内到期的事件并触发；剩余保留
        while True:
            due_idx = -1
            due_at = float("inf")
            for i, (at, _fn) in enumerate(self._schedule):
                if at <= target and at < due_at:
                    due_at = at
                    due_idx = i
            if due_idx < 0:
                break
            at, fn = self._schedule.pop(due_idx)
            self._t = at
            fn()
        self._t = target

    def schedule(self, at: float, fn: Callable[[], None]) -> None:
        """在虚拟时间 at 时刻触发 fn。at 必须 >= 当前虚拟时间。"""
        if at < self._t:
            raise ValueError(f"cannot schedule in the past (at={at}, now={self._t})")
        self._schedule.append((at, fn))

    def schedule_after(self, delta: float, fn: Callable[[], None]) -> None:
        self.schedule(self._t + delta, fn)

    def pending(self) -> int:
        return len(self._schedule)


# =========================================================================
# vn.py 事件 / 数据对象工厂
# =========================================================================


def make_contract_event(vt_symbol: str = "rb2510.SHFE") -> SimpleNamespace:
    """模拟 vn.py 在合约下发期间推送的 EVENT_CONTRACT。"""
    return SimpleNamespace(
        data=SimpleNamespace(vt_symbol=vt_symbol, symbol=vt_symbol.split(".")[0])
    )


def make_position_event(
    vt_symbol: str = "rb2510.SHFE", direction: str = "多", volume: int = 1
) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            vt_symbol=vt_symbol,
            direction=SimpleNamespace(value=direction),
            volume=volume,
        )
    )


def make_account_event(balance: float = 100_000.0) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(balance=balance, accountid="TEST", available=balance)
    )


def make_position(vt_symbol: str, direction: str, volume: int) -> SimpleNamespace:
    """模拟 main_engine.get_all_positions() 返回项。"""
    return SimpleNamespace(
        vt_symbol=vt_symbol,
        direction=SimpleNamespace(value=direction),
        volume=volume,
    )


# =========================================================================
# EventEngine 快速停机助手
# =========================================================================
#
# vn.py 的 EventEngine.stop() 会 join 两个线程：worker 在 ``queue.get(timeout=1)``
# 上最多阻塞 1 秒、timer 在 ``sleep(interval=1)`` 中最多再阻塞 1 秒。包含 MainEngine
# 的回放/SIT 测试每个 teardown ≈ 1s，整个套件因此被拖慢 ~15-18 秒。
#
# 这里提供两个针对测试场景的助手：
#   - ``make_test_event_engine()``：构造 ``interval=0.05`` 的 EventEngine，让 timer
#     线程在 ~50ms 内退出（默认 1s）。
#   - ``stop_event_engine_fast(ee)``：在 ``MainEngine.close()`` 之前调用，向队列推
#     一个 sentinel event 唤醒 worker；之后 ``MainEngine.close()`` 走的
#     ``EventEngine.stop()`` join 看到的就是已死/几乎已死的线程，瞬间返回。
#
# 不动 vn.py 源代码：MainEngine.close() 仍照旧调用 event_engine.stop()，只是被我们
# "提前缴械"了。生产代码路径 0 改动。
# =========================================================================


def make_test_event_engine(interval: float = 0.05) -> EventEngine:
    """构造一个 ``timer interval`` 小到能秒退的 EventEngine。

    默认 1 秒 timer 在 teardown 时是测试套件最大耗时来源；50ms 足以让所有依赖
    EVENT_TIMER 的逻辑（生产代码当前没有）保持行为不变，又能让 ``_timer`` 线程在
    停机后立刻退出。
    """
    from vnpy.event import EventEngine

    return EventEngine(interval=interval)


def stop_event_engine_fast(ee: EventEngine) -> None:
    """在 MainEngine.close() 之前唤醒 EventEngine worker 线程。

    实现细节
    --------
    EventEngine._run 在 ``self._queue.get(block=True, timeout=1)`` 上阻塞；只要
    队列非空（或 timeout 到期）才返回。我们 push 一个无 handler 的 sentinel
    event，worker 立刻取出、走一遍 ``_process``（找不到 handler → 跳过），下一轮
    while 循环看到 ``_active=False`` 就退出。

    幂等：``_active`` 已为 False 时直接返回。
    """
    if not getattr(ee, "_active", False):
        return
    ee._active = False
    try:
        from vnpy.event import Event

        ee._queue.put(Event(type="__test_shutdown__"))
    except Exception:
        # 测试场景：失败也不该影响测试结果，stop() 会兜底
        pass
