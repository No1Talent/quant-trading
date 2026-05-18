"""Reconciler 纯函数测试：is_settled 与 diff_positions。

这些测试零依赖、零时序、表驱动。微秒级跑完，永远不 flaky。
"""

from __future__ import annotations

import pytest

from utils.reconciler import _normalize_direction, diff_positions, is_settled


class TestIsSettled:
    @pytest.mark.parametrize(
        "last_ts, now, quiet_ms, expected",
        [
            (None, 10.0, 800, False),  # 从未收到事件 → 永远未静默
            (10.0, 10.0, 800, False),  # 刚发生 → 0ms 静默 < 800ms
            (10.0, 10.5, 800, False),  # 500ms 静默 < 800ms
            (10.0, 10.8, 800, True),  # 800ms 静默 = 阈值
            (10.0, 10.81, 800, True),  # 810ms 静默 > 阈值
            (10.0, 100.0, 800, True),  # 远远超过
            (10.0, 10.0, 0, True),  # 零阈值边界
        ],
    )
    def test_table(self, last_ts: float | None, now: float, quiet_ms: int, expected: bool):
        assert is_settled(last_ts, now, quiet_ms) is expected


class TestDiffPositions:
    def test_identical_returns_empty(self):
        local = {"rb2510.SHFE": ("LONG", 3)}
        ctp = {"rb2510.SHFE": ("LONG", 3)}
        assert diff_positions(local, ctp) == []

    def test_both_empty_returns_empty(self):
        assert diff_positions({}, {}) == []

    def test_volume_mismatch(self):
        local = {"rb2510.SHFE": ("LONG", 3)}
        ctp = {"rb2510.SHFE": ("LONG", 5)}
        rows = diff_positions(local, ctp)
        assert rows == [{"vt_symbol": "rb2510.SHFE", "local": ("LONG", 3), "ctp": ("LONG", 5)}]

    def test_direction_mismatch(self):
        local = {"rb2510.SHFE": ("LONG", 3)}
        ctp = {"rb2510.SHFE": ("SHORT", 3)}
        rows = diff_positions(local, ctp)
        assert len(rows) == 1
        assert rows[0]["local"] == ("LONG", 3)
        assert rows[0]["ctp"] == ("SHORT", 3)

    def test_local_has_extra(self):
        local = {"rb2510.SHFE": ("LONG", 3)}
        ctp: dict = {}
        rows = diff_positions(local, ctp)
        assert rows == [{"vt_symbol": "rb2510.SHFE", "local": ("LONG", 3), "ctp": None}]

    def test_ctp_has_extra(self):
        local: dict = {}
        ctp = {"rb2510.SHFE": ("LONG", 3)}
        rows = diff_positions(local, ctp)
        assert rows == [{"vt_symbol": "rb2510.SHFE", "local": None, "ctp": ("LONG", 3)}]

    def test_multiple_mismatches_sorted(self):
        local = {"b.SHFE": ("LONG", 1), "a.SHFE": ("LONG", 1)}
        ctp = {"b.SHFE": ("LONG", 2), "a.SHFE": ("LONG", 2)}
        rows = diff_positions(local, ctp)
        # 输出按 vt_symbol 字典序，便于人工核对
        assert [r["vt_symbol"] for r in rows] == ["a.SHFE", "b.SHFE"]


class TestNormalizeDirection:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("多", "LONG"),
            ("空", "SHORT"),
            ("long", "LONG"),
            ("Long", "LONG"),
            ("short", "SHORT"),
            ("SHORT", "SHORT"),
            ("Direction.LONG", "LONG"),
            ("净", "NET"),
            ("unknown", "NET"),
        ],
    )
    def test_strings(self, raw: str, expected: str):
        assert _normalize_direction(raw) == expected

    def test_enum_with_value_attr(self):
        from types import SimpleNamespace

        assert _normalize_direction(SimpleNamespace(value="多")) == "LONG"
        assert _normalize_direction(SimpleNamespace(value="空")) == "SHORT"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
