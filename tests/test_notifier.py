"""Notifier 单元测试：单例、去重/限流并发、flush、各渠道。"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from utils.notifier import (
    NullNotifier,
    WebhookNotifier,
    get_notifier,
    reset_notifier,
    set_notifier,
)


@pytest.fixture
def mock_config():
    """空配置，所有渠道禁用 — 用于测试核心逻辑。"""
    return {
        "dedup_window_seconds": 60,
        "rate_limit_per_minute": 30,
        "email": {"enabled": False},
        "wechat_work": {"enabled": False},
        "server_chan": {"enabled": False},
        "dingtalk": {"enabled": False},
    }


@pytest.fixture(autouse=True)
def cleanup():
    reset_notifier()
    yield
    reset_notifier()


class TestSingleton:
    def test_get_notifier_returns_same_instance(self):
        n1 = get_notifier()
        n2 = get_notifier()
        assert n1 is n2

    def test_set_notifier_replaces_instance(self):
        mock = NullNotifier()
        set_notifier(mock)
        assert get_notifier() is mock

    def test_concurrent_get_notifier_thread_safe(self):
        results = []

        def worker():
            results.append(id(get_notifier()))

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == 1


class TestNullNotifier:
    def test_send_no_side_effect(self):
        n = NullNotifier()
        n.send("test")
        n.send_trade("strategy", {})
        n.send_signal("strategy", "signal")
        n.send_warning("strategy", "warn")
        n.send_error("strategy", "error")
        n.send_critical("critical")
        n.send_daily_report("report")
        n.flush()


class TestDeduplication:
    def test_duplicate_within_window_skipped(self, mock_config):
        n = WebhookNotifier(mock_config)

        assert not n._is_duplicate("hello")
        assert n._is_duplicate("hello")
        assert not n._is_duplicate("world")

    def test_concurrent_dedup_thread_safe(self, mock_config):
        n = WebhookNotifier(mock_config)

        results: list[bool | str] = []

        def worker(i):
            try:
                result = n._is_duplicate(f"msg_{i % 10}")
                results.append(result)
            except Exception as e:
                results.append(f"ERROR: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        errors = [r for r in results if isinstance(r, str) and r.startswith("ERROR")]
        assert len(errors) == 0


class TestRateLimit:
    def test_rate_limit_blocks_excess(self, mock_config):
        mock_config["rate_limit_per_minute"] = 5
        n = WebhookNotifier(mock_config)

        for _ in range(5):
            assert n._check_rate_limit()

        assert not n._check_rate_limit()

    def test_concurrent_rate_limit_thread_safe(self, mock_config):
        n = WebhookNotifier(mock_config)

        def worker():
            for _ in range(10):
                n._check_rate_limit()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()


class TestFlush:
    def test_flush_waits_for_pending_tasks(self, mock_config):
        n = WebhookNotifier(mock_config)

        slow_calls = []

        def slow_dispatch(*args):
            time.sleep(0.5)
            slow_calls.append(time.time())

        n._dispatch = slow_dispatch  # type: ignore[method-assign]

        for i in range(3):
            n.send(f"msg_{i}", force=True)

        start = time.time()
        n.flush()
        elapsed = time.time() - start

        assert elapsed >= 0.3
        assert len(slow_calls) == 3

    def test_shutdown_after_flush_rejects_new_messages(self, mock_config):
        n = WebhookNotifier(mock_config)
        n._shutdown_flag = True

        n._dispatch = MagicMock()  # type: ignore[method-assign]
        n.send("test")
        time.sleep(0.1)

        n._dispatch.assert_not_called()


class TestChannels:
    def test_wechat_work_called_when_enabled(self, mock_config):
        mock_config["wechat_work"] = {"enabled": True, "webhook": "https://example.com/webhook"}
        n = WebhookNotifier(mock_config)

        with patch.object(n.session, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"errcode": 0}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            n.send("test message", force=True)
            n.flush()

            assert mock_post.called

    def test_channel_failure_isolated(self, mock_config):
        mock_config["wechat_work"] = {"enabled": True, "webhook": "https://invalid"}
        mock_config["dingtalk"] = {"enabled": True, "webhook": "https://example.com/dingtalk"}
        n = WebhookNotifier(mock_config)

        def mock_post(url, **kwargs):
            response = MagicMock()
            if "invalid" in url:
                raise RuntimeError("Network error")
            response.json.return_value = {"errcode": 0}
            response.raise_for_status = MagicMock()
            return response

        with patch.object(n.session, "post", side_effect=mock_post):
            n.send("test", force=True)
            n.flush()


class TestRecursionPrevention:
    def test_safe_call_swallows_exception(self, mock_config):
        n = WebhookNotifier(mock_config)

        def failing_func():
            raise RuntimeError("network error")

        n._safe_call(failing_func)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
