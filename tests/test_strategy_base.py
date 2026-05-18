"""safe_callback decorator: exception swallowing, write_log routing, @wraps preservation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from utils.strategy_base import safe_callback


class TestSafeCallback:
    def test_normal_execution_returns_value(self):
        class S:
            @safe_callback
            def on_bar(self, x):
                return x * 2

        assert S().on_bar(5) == 10

    def test_exception_invokes_write_log(self):
        class S:
            def __init__(self):
                self.write_log = MagicMock()

            @safe_callback
            def on_bar(self, x):
                raise ValueError("bad bar")

        s = S()
        assert s.on_bar(1) is None
        s.write_log.assert_called_once()
        msg = s.write_log.call_args[0][0]
        assert "on_bar" in msg
        assert "bad bar" in msg

    def test_exception_falls_back_to_module_logger_when_no_write_log(self):
        class S:
            @safe_callback
            def on_tick(self, x):
                raise RuntimeError("boom")

        with patch("utils.strategy_base.logger") as mock_logger:
            assert S().on_tick(1) is None
        mock_logger.error.assert_called_once()

    def test_wraps_preserves_function_name(self):
        class S:
            @safe_callback
            def on_bar(self, x):
                return x

        assert S.on_bar.__name__ == "on_bar"
