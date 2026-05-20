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
        "feishu": {"enabled": False},
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


class TestFeishu:
    """飞书渠道：HMAC 签名走的是 timestamp+\"\\n\"+secret 作为 key 对空消息体签 SHA256
    再 base64 —— 这与官方文档一致，错一个字节就 19021。"""

    def test_feishu_unsigned_payload_shape(self, mock_config):
        mock_config["feishu"] = {
            "enabled": True,
            "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
        }
        n = WebhookNotifier(mock_config)

        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"code": 0, "msg": "ok"}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp

            n.send("hello", force=True)
            n.flush()

            assert mock_post.called
            payload = mock_post.call_args.kwargs["json"]
            assert payload["msg_type"] == "text"
            assert "hello" in payload["content"]["text"]
            # 未配置 secret 时不带 timestamp/sign
            assert "timestamp" not in payload
            assert "sign" not in payload

    def test_feishu_signed_payload_matches_official_algo(self, mock_config):
        """复现官方签名算法，比对模块实际输出。任何漂移都会被这条捕获。"""
        import base64
        import hashlib
        import hmac

        secret = "TestSecretForFeishuBot"
        mock_config["feishu"] = {
            "enabled": True,
            "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/yyy",
            "secret": secret,
        }
        n = WebhookNotifier(mock_config)

        captured: dict = {}

        def _capture(url, **kwargs):
            captured.update(kwargs["json"])
            resp = MagicMock()
            resp.json.return_value = {"code": 0, "msg": "ok"}
            resp.raise_for_status = MagicMock()
            return resp

        with patch.object(n.session, "post", side_effect=_capture):
            n.send("payload", force=True)
            n.flush()

        assert "timestamp" in captured and "sign" in captured
        # 重算官方算法：key = timestamp\nsecret, msg = empty, sha256, base64
        string_to_sign = f"{captured['timestamp']}\n{secret}"
        expected = base64.b64encode(
            hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        assert captured["sign"] == expected

    def test_feishu_at_all_wraps_message(self, mock_config):
        mock_config["feishu"] = {
            "enabled": True,
            "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/zzz",
            "at_all": True,
        }
        n = WebhookNotifier(mock_config)

        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"code": 0, "msg": "ok"}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp

            n.send("urgent", force=True)
            n.flush()

            text = mock_post.call_args.kwargs["json"]["content"]["text"]
            assert '<at user_id="all">' in text
            assert "urgent" in text

    def test_feishu_v1_status_code_schema_accepted(self, mock_config):
        """Feishu API v1 返回 StatusCode=0；v2 返回 code=0。当前实现两个 schema 都视为成功。"""
        mock_config["feishu"] = {"enabled": True, "webhook": "https://example.com/feishu"}
        n = WebhookNotifier(mock_config)

        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"StatusCode": 0, "StatusMessage": "success"}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp

            # 不应抛 — RuntimeError 会被 _safe_call 吞掉，所以这里直接调 _send_feishu
            n._send_feishu("title", "body")  # 不抛即为通过

    def test_feishu_error_response_raises(self, mock_config):
        mock_config["feishu"] = {"enabled": True, "webhook": "https://example.com/feishu"}
        n = WebhookNotifier(mock_config)

        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"code": 19021, "msg": "sign match fail"}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp

            with pytest.raises(RuntimeError, match="飞书API错误"):
                n._send_feishu("title", "body")


class TestEmail:
    """SMTP send path: port-465 → SMTP_SSL, otherwise SMTP+STARTTLS. UTF-8 Header on subject."""

    def _cfg(self, port: int = 465) -> dict:
        return {
            "email": {
                "enabled": True,
                "server": "smtp.example.com",
                "port": port,
                "sender": "bot@example.com",
                "receiver": "ops@example.com",
                "username": "bot@example.com",
                "password": "secret",
            },
        }

    def test_email_ssl_path_uses_smtp_ssl(self, mock_config):
        mock_config.update(self._cfg(port=465))
        n = WebhookNotifier(mock_config)

        with (
            patch("utils.notifier.smtplib.SMTP_SSL") as ssl_cls,
            patch("utils.notifier.smtplib.SMTP") as plain_cls,
        ):
            ssl_instance = MagicMock()
            ssl_cls.return_value.__enter__.return_value = ssl_instance
            n._send_email("启动通知", "body")
            ssl_cls.assert_called_once_with("smtp.example.com", 465, timeout=10)
            plain_cls.assert_not_called()
            ssl_instance.login.assert_called_once_with("bot@example.com", "secret")
            ssl_instance.send_message.assert_called_once()

    def test_email_starttls_path_for_port_587(self, mock_config):
        mock_config.update(self._cfg(port=587))
        n = WebhookNotifier(mock_config)

        with (
            patch("utils.notifier.smtplib.SMTP_SSL") as ssl_cls,
            patch("utils.notifier.smtplib.SMTP") as plain_cls,
        ):
            plain_instance = MagicMock()
            plain_cls.return_value.__enter__.return_value = plain_instance
            n._send_email("启动通知", "body")
            plain_cls.assert_called_once_with("smtp.example.com", 587, timeout=10)
            ssl_cls.assert_not_called()
            plain_instance.starttls.assert_called_once()
            plain_instance.login.assert_called_once_with("bot@example.com", "secret")
            plain_instance.send_message.assert_called_once()

    def test_email_subject_carries_utf8_chinese(self, mock_config):
        """Non-ASCII subjects must round-trip via email.header.Header — bare strings get
        mangled to '?' by some SMTP servers."""
        mock_config.update(self._cfg(port=465))
        n = WebhookNotifier(mock_config)
        with patch("utils.notifier.smtplib.SMTP_SSL") as ssl_cls:
            ssl_instance = MagicMock()
            ssl_cls.return_value.__enter__.return_value = ssl_instance
            n._send_email("启动通知🚨", "body")
            sent_msg = ssl_instance.send_message.call_args.args[0]
            # The Subject header must encode as UTF-8 base64 — confirms Header usage
            assert "utf-8" in str(sent_msg["Subject"]).lower() or "启动通知" in str(
                sent_msg["Subject"]
            )


class TestDingTalk:
    """DingTalk text webhook: msgtype=text, optional at_mobiles list + isAtAll boolean."""

    def _cfg(self, **overrides) -> dict:
        cfg = {
            "dingtalk": {
                "enabled": True,
                "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
            },
        }
        cfg["dingtalk"].update(overrides)
        return cfg

    def test_dingtalk_payload_shape_minimal(self, mock_config):
        mock_config.update(self._cfg())
        n = WebhookNotifier(mock_config)
        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"errcode": 0}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp
            n._send_dingtalk("ignored title", "hello world")
            url, kwargs = mock_post.call_args.args[0], mock_post.call_args.kwargs
            assert url == mock_config["dingtalk"]["webhook"]
            payload = kwargs["json"]
            assert payload["msgtype"] == "text"
            assert payload["text"]["content"] == "hello world"
            # Defaults: empty at_mobiles, isAtAll False
            assert payload["at"]["atMobiles"] == []
            assert payload["at"]["isAtAll"] is False

    def test_dingtalk_at_mobiles_propagates(self, mock_config):
        mock_config.update(self._cfg(at_mobiles=["13800138000"], at_all=True))
        n = WebhookNotifier(mock_config)
        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"errcode": 0}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp
            n._send_dingtalk("t", "msg")
            payload = mock_post.call_args.kwargs["json"]
            assert payload["at"]["atMobiles"] == ["13800138000"]
            assert payload["at"]["isAtAll"] is True

    def test_dingtalk_error_response_raises(self, mock_config):
        mock_config.update(self._cfg())
        n = WebhookNotifier(mock_config)
        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            # DingTalk uses errcode (non-zero = error)
            resp.json.return_value = {"errcode": 310000, "errmsg": "keywords not in content"}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp
            with pytest.raises(RuntimeError, match="钉钉API错误"):
                n._send_dingtalk("t", "msg")


class TestServerChan:
    """Server酱 (ftqq): URL is sctapi.ftqq.com/<sendkey>.send, form-encoded title + desp."""

    def _cfg(self, sendkey: str = "SCT123ABC") -> dict:
        return {"server_chan": {"enabled": True, "sendkey": sendkey}}

    def test_server_chan_url_includes_sendkey(self, mock_config):
        mock_config.update(self._cfg(sendkey="SCT123ABC"))
        n = WebhookNotifier(mock_config)
        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"code": 0}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp
            n._send_server_chan("启动", "system ready")
            url = mock_post.call_args.args[0]
            assert url == "https://sctapi.ftqq.com/SCT123ABC.send"

    def test_server_chan_form_encoded_title_and_desp(self, mock_config):
        mock_config.update(self._cfg())
        n = WebhookNotifier(mock_config)
        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"code": 0}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp
            n._send_server_chan("subj", "body")
            # Server酱 uses form-encoded body (data=...), not JSON
            assert mock_post.call_args.kwargs["data"] == {"title": "subj", "desp": "body"}
            assert "json" not in mock_post.call_args.kwargs

    def test_server_chan_error_response_raises(self, mock_config):
        mock_config.update(self._cfg())
        n = WebhookNotifier(mock_config)
        with patch.object(n.session, "post") as mock_post:
            resp = MagicMock()
            resp.json.return_value = {"code": 40001, "message": "sendkey invalid"}
            resp.raise_for_status = MagicMock()
            mock_post.return_value = resp
            with pytest.raises(RuntimeError, match="Server酱API错误"):
                n._send_server_chan("t", "b")


class TestRecursionPrevention:
    def test_safe_call_swallows_exception(self, mock_config):
        n = WebhookNotifier(mock_config)

        def failing_func():
            raise RuntimeError("network error")

        n._safe_call(failing_func)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
