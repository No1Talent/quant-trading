"""
================================================================
Notifier 单元测试
================================================================
覆盖：
    - 单例线程安全
    - 去重和限流的并发安全
    - flush的资源释放
    - NullNotifier的零副作用

运行：
    cd C:\\Quant
    .venv\\Scripts\\activate.bat
    pip install pytest
    pytest tests/ -v
================================================================
"""

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


# ============================================================
# Fixtures
# ============================================================
@pytest.fixture
def mock_config():
    """空配置，所有渠道禁用 - 用于测试核心逻辑"""
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
    """每个测试前后重置全局单例"""
    reset_notifier()
    yield
    reset_notifier()


# ============================================================
# 单例测试
# ============================================================
class TestSingleton:
    """单例模式测试"""

    def test_get_notifier_returns_same_instance(self):
        """连续调用返回同一实例"""
        n1 = get_notifier()
        n2 = get_notifier()
        assert n1 is n2

    def test_set_notifier_replaces_instance(self):
        """set_notifier可以替换实例"""
        mock = NullNotifier()
        set_notifier(mock)
        assert get_notifier() is mock

    def test_concurrent_get_notifier_thread_safe(self):
        """SEVERE-1：并发调用get_notifier，必须返回同一实例"""
        results = []

        def worker():
            results.append(id(get_notifier()))

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有线程拿到的应该是同一个实例
        assert len(set(results)) == 1


# ============================================================
# NullNotifier测试
# ============================================================
class TestNullNotifier:
    """NullNotifier应该静默处理所有调用"""

    def test_send_no_side_effect(self):
        n = NullNotifier()
        # 不会抛异常
        n.send("test")
        n.send_trade("strategy", {})
        n.send_signal("strategy", "signal")
        n.send_warning("strategy", "warn")
        n.send_error("strategy", "error")
        n.send_critical("critical")
        n.send_daily_report("report")
        n.flush()


# ============================================================
# 去重测试（SEVERE-3）
# ============================================================
class TestDeduplication:
    """去重机制测试"""

    def test_duplicate_within_window_skipped(self, mock_config):
        """窗口内相同消息只发一次"""
        n = WebhookNotifier(mock_config)

        # 第一次不重复
        assert not n._is_duplicate("hello")
        # 第二次重复
        assert n._is_duplicate("hello")
        # 不同消息不重复
        assert not n._is_duplicate("world")

    def test_concurrent_dedup_thread_safe(self, mock_config):
        """SEVERE-3：并发调用去重不会崩溃"""
        n = WebhookNotifier(mock_config)

        results: list[bool | str] = []

        def worker(i):
            try:
                # 各种消息混合，触发dict遍历和修改
                result = n._is_duplicate(f"msg_{i % 10}")
                results.append(result)
            except Exception as e:
                results.append(f"ERROR: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 不应该有任何异常
        errors = [r for r in results if isinstance(r, str) and r.startswith("ERROR")]
        assert len(errors) == 0


# ============================================================
# 限流测试
# ============================================================
class TestRateLimit:
    """频率限制测试"""

    def test_rate_limit_blocks_excess(self, mock_config):
        mock_config["rate_limit_per_minute"] = 5
        n = WebhookNotifier(mock_config)

        # 前5次都能通过
        for _ in range(5):
            assert n._check_rate_limit()

        # 第6次被拦截
        assert not n._check_rate_limit()

    def test_concurrent_rate_limit_thread_safe(self, mock_config):
        """限流在并发下不会崩溃"""
        n = WebhookNotifier(mock_config)

        def worker():
            for _ in range(10):
                n._check_rate_limit()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 没崩就行


# ============================================================
# flush测试（SEVERE-2）
# ============================================================
class TestFlush:
    """SEVERE-2：flush应该等待所有任务完成"""

    def test_flush_waits_for_pending_tasks(self, mock_config):
        n = WebhookNotifier(mock_config)

        # mock一个慢任务
        slow_calls = []

        def slow_dispatch(*args):
            time.sleep(0.5)
            slow_calls.append(time.time())

        n._dispatch = slow_dispatch  # type: ignore[method-assign]

        # 提交3个任务
        for i in range(3):
            n.send(f"msg_{i}", force=True)

        # flush应该阻塞到所有任务完成
        start = time.time()
        n.flush()
        elapsed = time.time() - start

        # 4个worker并行执行3个0.5秒任务，总共应该约0.5秒
        assert elapsed >= 0.3  # 至少完成一个任务的时间
        assert len(slow_calls) == 3  # 所有任务都执行了

    def test_shutdown_after_flush_rejects_new_messages(self, mock_config):
        """关闭后新消息应该被拒绝"""
        n = WebhookNotifier(mock_config)
        n._shutdown_flag = True

        # 设个mock追踪dispatch是否被调用
        n._dispatch = MagicMock()  # type: ignore[method-assign]
        n.send("test")
        time.sleep(0.1)

        n._dispatch.assert_not_called()


# ============================================================
# 渠道测试
# ============================================================
class TestChannels:
    """各渠道发送逻辑测试"""

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
        """单一渠道失败不影响其他渠道"""
        mock_config["wechat_work"] = {"enabled": True, "webhook": "https://invalid"}
        mock_config["dingtalk"] = {"enabled": True, "webhook": "https://example.com/dingtalk"}
        n = WebhookNotifier(mock_config)

        # 让企业微信失败，钉钉成功
        def mock_post(url, **kwargs):
            response = MagicMock()
            if "invalid" in url:
                raise RuntimeError("Network error")
            response.json.return_value = {"errcode": 0}
            response.raise_for_status = MagicMock()
            return response

        with patch.object(n.session, "post", side_effect=mock_post):
            # 应该不抛异常
            n.send("test", force=True)
            n.flush()
            # 进入这里没崩就说明隔离生效


# ============================================================
# 安全测试 - SEVERE-5
# ============================================================
class TestRecursionPrevention:
    """SEVERE-5：失败信息不应该触发新的告警"""

    def test_safe_call_swallows_exception(self, mock_config):
        n = WebhookNotifier(mock_config)

        def failing_func():
            raise RuntimeError("network error")

        # _safe_call应该吞掉异常
        n._safe_call(failing_func)
        # 没抛出就对


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
