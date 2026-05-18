"""测试装置：FakeClock 与 vn.py 事件工厂。

FakeClock 是 reconciler 测试的核心 — 把时间从全局副作用变为显式依赖，让单线程的
sleep() 调用同时承担"推进虚拟时间"与"按时序触发预定回调"两个职责。
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace


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
