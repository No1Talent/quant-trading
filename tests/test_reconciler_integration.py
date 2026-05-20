"""Reconciler 集成 smoke：用真实 time.sleep + 极小窗口跑一次 wiring。

只放一个测试 — 验证生产路径下 clock=time.monotonic / sleeper=time.sleep 的协同确实通。
真实时序意味着允许偶发 flaky；用 reruns=2 兜底，1 个测试可控。

通过 `@pytest.mark.slow` 标记后默认 pre-commit 跳过，需要时显式 `pytest -m slow` 运行。
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from tests._fakes import make_account_event, make_position, make_position_event
from utils.notifier import NullNotifier
from utils.reconciler import CtpReconciler


@pytest.mark.slow
def test_real_clock_smoke(tmp_path):
    """真实时钟下：query_position 触发后 30ms 推一条 position，等 50ms 静默通过。"""
    me = MagicMock()
    # query_position/query_account 走 gateway，别名到 me 简化测试
    gw = MagicMock()
    me.get_gateway.return_value = gw
    me.query_position = gw.query_position
    me.query_account = gw.query_account
    ee = MagicMock()

    r = CtpReconciler(
        main_engine=me,
        event_engine=ee,
        notifier=NullNotifier(),
        init_quiet_ms=50,
        init_safety_margin_s=0.05,
        settle_quiet_ms=50,
        hard_timeout_s=3.0,
        poll_interval_s=0.01,
        breach_flag_path=tmp_path / "rec.flag",
    )

    def fire_position_later():
        t = threading.Timer(
            0.03, lambda: r._on_position(make_position_event("rb2510.SHFE", "多", 1))
        )
        t.daemon = True
        t.start()

    def fire_account_later():
        t = threading.Timer(0.03, lambda: r._on_account(make_account_event()))
        t.daemon = True
        t.start()

    me.query_position.side_effect = fire_position_later
    me.query_account.side_effect = fire_account_later
    me.get_all_positions.return_value = [make_position("rb2510.SHFE", "多", 1)]

    start = time.monotonic()
    diff = r.reconcile_against({"rb2510.SHFE": ("LONG", 1)})
    elapsed = time.monotonic() - start

    assert diff == []
    # 至少跨过：init_quiet(50ms) + safety(50ms) + pos_settle(50ms) + acct_settle(50ms) ≈ 200ms
    assert elapsed >= 0.15
    # 上限宽松一点，防 CI 慢机器导致 flaky；3s 已经远大于预期
    assert elapsed < 3.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "slow"])
