"""
通用消息推送模块

设计原则：
    - 异步发送：线程池不阻塞策略主线程
    - 优雅退出：进程结束前等待所有在途消息发送完成
    - 失败隔离：单一渠道失败不影响其他渠道
    - 防风暴：去重+限流+递归防护
"""

import atexit
import json
import logging
import os
import smtplib
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("notifier")
logger.setLevel(logging.INFO)

if not logger.handlers:
    _console = logging.StreamHandler()
    _console.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] [Notifier] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    logger.addHandler(_console)

    try:
        log_dir = Path(__file__).parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        _file = logging.FileHandler(log_dir / "notifier.log", encoding="utf-8")
        _file.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(_file)
    except Exception:
        pass


class NotifyLevel(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class INotifier:
    """通知器接口 - 策略代码应该依赖此接口，不依赖具体实现"""

    def send(
        self,
        message: str,
        title: str = "",
        level: NotifyLevel = NotifyLevel.INFO,
        force: bool = False,
    ) -> None:
        raise NotImplementedError

    def send_trade(self, strategy_name: str, trade_info: dict) -> None:
        raise NotImplementedError

    def send_signal(self, strategy_name: str, signal: str, detail: str = "") -> None:
        raise NotImplementedError

    def send_warning(self, strategy_name: str, warning: str) -> None:
        raise NotImplementedError

    def send_error(
        self, strategy_name: str, error: str, exception: Exception | None = None
    ) -> None:
        raise NotImplementedError

    def send_critical(self, message: str) -> None:
        raise NotImplementedError

    def send_daily_report(self, report: str) -> None:
        raise NotImplementedError

    def flush(self, timeout: float = 10.0) -> None:
        pass


class NullNotifier(INotifier):
    """回测时注入，所有通知静默"""

    def send(self, *args, **kwargs):
        pass

    def send_trade(self, *args, **kwargs):
        pass

    def send_signal(self, *args, **kwargs):
        pass

    def send_warning(self, *args, **kwargs):
        pass

    def send_error(self, *args, **kwargs):
        pass

    def send_critical(self, *args, **kwargs):
        pass

    def send_daily_report(self, *args, **kwargs):
        pass

    def flush(self, timeout: float = 10.0):
        pass


class WebhookNotifier(INotifier):
    """基于HTTP/SMTP的真实通知器实现"""

    # (channel_key, display_label) — order determines dispatch order
    _CHANNEL_DEFS: list[tuple[str, str]] = [
        ("email", "邮件"),
        ("wechat_work", "企业微信"),
        ("server_chan", "Server酱"),
        ("dingtalk", "钉钉"),
    ]

    def __init__(self, config: dict):
        self.config = config

        self._dedup_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()

        self.recent_messages: dict[int, float] = {}
        self.dedup_window = config.get("dedup_window_seconds", 60)

        self.rate_limit = config.get("rate_limit_per_minute", 30)
        self.send_timestamps: deque[float] = deque()

        # Cache static config so _dispatch doesn't re-parse per message
        self._level_routing: dict = config.get("level_routing", {})

        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notifier")
        self._shutdown_flag = False

        self.session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["POST", "GET"],
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        atexit.register(self._shutdown_handler)

        logger.info("通知器初始化完成，启用渠道: %s", self._get_enabled_channels())

    def _get_enabled_channels(self) -> list[str]:
        return [
            label for key, label in self._CHANNEL_DEFS if self.config.get(key, {}).get("enabled")
        ]

    # ========================================================
    # 公开接口
    # ========================================================

    def send(
        self,
        message: str,
        title: str = "vn.py交易通知",
        level: NotifyLevel = NotifyLevel.INFO,
        force: bool = False,
    ):
        # SEVERE-2: 关闭后拒绝新消息
        if self._shutdown_flag:
            logger.warning("通知器已关闭，丢弃消息: %s", message[:30])
            return

        # 频率限制
        if not force and not self._check_rate_limit():
            logger.warning("频率超限，丢弃消息: %s", message[:30])
            return

        # 去重
        if not force and self._is_duplicate(message):
            return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_message = f"[{level.value}] [{timestamp}]\n{message}"

        with self._shutdown_lock:
            if self._shutdown_flag:
                logger.warning("通知器已关闭，丢弃消息: %s", message[:30])
                return
            try:
                self.executor.submit(self._dispatch, title, full_message, level)
            except RuntimeError as e:
                logger.error("提交任务失败（线程池可能已关闭）: %s", e)

    def send_trade(self, strategy_name: str, trade_info: dict):
        emoji = "🟢" if trade_info.get("direction") == "多" else "🔴"
        message = (
            f"{emoji} 策略成交\n"
            f"━━━━━━━━━━━━━━\n"
            f"策略：{strategy_name}\n"
            f"合约：{trade_info.get('symbol', 'N/A')}\n"
            f"方向：{trade_info.get('direction', 'N/A')} {trade_info.get('offset', '')}\n"
            f"价格：{trade_info.get('price', 0):.2f}\n"
            f"数量：{trade_info.get('volume', 0)}手\n"
            f"时间：{trade_info.get('datetime', datetime.now())}"
        )
        self.send(message, title=f"成交-{strategy_name}", level=NotifyLevel.INFO)

    def send_signal(self, strategy_name: str, signal: str, detail: str = ""):
        message = f"📊 策略信号\n━━━━━━━━━━━━━━\n策略：{strategy_name}\n信号：{signal}\n{detail}"
        self.send(message, title=f"信号-{strategy_name}", level=NotifyLevel.INFO)

    def send_warning(self, strategy_name: str, warning: str):
        message = f"⚠️ 策略警告\n策略：{strategy_name}\n内容：{warning}"
        self.send(message, title=f"警告-{strategy_name}", level=NotifyLevel.WARNING, force=True)

    def send_error(self, strategy_name: str, error: str, exception: Exception | None = None):
        message = f"❌ 策略错误\n策略：{strategy_name}\n错误：{error}"
        if exception:
            tb = traceback.format_exc()
            message += f"\n\n堆栈信息：\n{tb}"
        self.send(message, title=f"错误-{strategy_name}", level=NotifyLevel.ERROR, force=True)

    def send_critical(self, message: str):
        full_msg = f"🚨🚨🚨 严重告警\n{message}"
        self.send(full_msg, title="严重告警", level=NotifyLevel.CRITICAL, force=True)

    def send_daily_report(self, report: str):
        self.send(report, title="日报", level=NotifyLevel.INFO, force=True)

    # ========================================================
    # SEVERE-2：优雅关闭
    # ========================================================

    def flush(self, timeout: float = 10.0):
        """
        阻塞等待所有在途消息发送完成
        业务代码在关键节点（如系统关闭前）调用
        """
        logger.info("等待所有通知发送完成（最多%s秒）...", timeout)

        with self._shutdown_lock:
            old_executor = self.executor
            self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notifier")

        # shutdown(wait=True)等待已提交的任务完成
        # 不传timeout是因为3.9+才支持
        old_executor.shutdown(wait=True)
        logger.info("通知发送完成")

    def _shutdown_handler(self):
        """atexit回调：进程退出时优雅关闭"""
        if self._shutdown_flag:
            return
        self._shutdown_flag = True
        # 解释器退出阶段 stdio（或 pytest 捕获流）可能已被关闭，
        # StreamHandler.emit 会失败并经 handleError 把 traceback 直接写 stderr，
        # 绕过 try/except。用官方开关 raiseExceptions 静默掉 handler 异常。
        logging.raiseExceptions = False
        try:
            logger.info("进程退出，等待通知发送完成...")
            self.executor.shutdown(wait=True)
            self.session.close()
        except Exception:
            pass

    # ========================================================
    # SEVERE-3：线程安全的辅助方法
    # ========================================================

    def _check_rate_limit(self) -> bool:
        now = time.time()
        with self._rate_lock:
            while self.send_timestamps and now - self.send_timestamps[0] >= 60:
                self.send_timestamps.popleft()
            if len(self.send_timestamps) >= self.rate_limit:
                return False
            self.send_timestamps.append(now)
        return True

    def _is_duplicate(self, message: str) -> bool:
        msg_hash = hash(message)
        now = time.time()
        with self._dedup_lock:
            # 用列表推导生成新dict避免迭代中修改
            self.recent_messages = {
                k: v for k, v in self.recent_messages.items() if now - v < self.dedup_window
            }
            if msg_hash in self.recent_messages:
                return True
            self.recent_messages[msg_hash] = now
        return False

    # ========================================================
    # 分发逻辑
    # ========================================================

    def _dispatch(self, title: str, message: str, level: NotifyLevel):
        enabled = self._level_routing.get(level.value, ["all"])
        wildcard = "all" in enabled
        for key, _label in self._CHANNEL_DEFS:
            if not (wildcard or key in enabled):
                continue
            if not self.config.get(key, {}).get("enabled"):
                continue
            sender = getattr(self, f"_send_{key}")
            self._safe_call(sender, title, message)

    def _safe_call(self, func: Callable, *args):
        """
        容错调用 - SEVERE-5: 失败信息只走logging，不走LOG事件
        防止递归告警
        """
        try:
            func(*args)
        except Exception as e:
            # ⚠️ 关键：用logger记录到文件/控制台，绝不能调用任何会产生LOG事件的接口
            logger.error("%s 失败: %s", func.__name__, e)

    # ========================================================
    # 各渠道实现
    # ========================================================

    def _send_email(self, title: str, message: str):
        cfg = self.config["email"]

        msg = MIMEMultipart()
        msg["From"] = cfg["sender"]
        msg["To"] = cfg["receiver"]
        msg["Subject"] = Header(title, "utf-8")
        msg.attach(MIMEText(message, "plain", "utf-8"))

        port = cfg.get("port", 465)
        if port == 465:
            with smtplib.SMTP_SSL(cfg["server"], port, timeout=10) as smtp:
                smtp.login(cfg["username"], cfg["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg["server"], port, timeout=10) as smtp:
                smtp.starttls()
                smtp.login(cfg["username"], cfg["password"])
                smtp.send_message(msg)

        logger.info("邮件发送成功 -> %s", cfg["receiver"])

    def _send_wechat_work(self, title: str, message: str):
        del title  # API 无独立标题字段
        cfg = self.config["wechat_work"]
        url = cfg["webhook"]
        payload = {
            "msgtype": "text",
            "text": {"content": message, "mentioned_mobile_list": cfg.get("mentioned_mobile", [])},
        }
        resp = self.session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"企业微信API错误: {result}")
        logger.info("企业微信推送成功")

    def _send_server_chan(self, title: str, message: str):
        cfg = self.config["server_chan"]
        sendkey = cfg["sendkey"]
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        payload = {"title": title, "desp": message}
        resp = self.session.post(url, data=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(f"Server酱API错误: {result}")
        logger.info("Server酱推送成功")

    def _send_dingtalk(self, title: str, message: str):
        del title  # 钉钉文本消息无独立标题字段
        cfg = self.config["dingtalk"]
        url = cfg["webhook"]
        payload = {
            "msgtype": "text",
            "text": {"content": message},
            "at": {"atMobiles": cfg.get("at_mobiles", []), "isAtAll": cfg.get("at_all", False)},
        }
        resp = self.session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"钉钉API错误: {result}")
        logger.info("钉钉推送成功")


# ============================================================
# SEVERE-1：模块级单例（替代不安全的__new__）
# ============================================================
_notifier_instance: INotifier | None = None
_notifier_lock = threading.Lock()


def _load_config(path: str) -> dict:
    """加载配置文件，支持环境变量覆盖"""
    # 优先加载真实配置
    p = Path(path)
    if not p.exists():
        template = p.with_suffix(".json.template")
        if template.exists():
            logger.warning("配置文件不存在 %s，请复制 %s 并填入真实凭据", p.name, template.name)
        else:
            logger.warning("配置文件不存在 %s，所有通知渠道未启用", path)
        return {}

    try:
        with open(p, encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("配置文件JSON格式错误: %s", e)
        return {}

    # 环境变量覆盖敏感字段（SEVERE-4配套）
    # 优先级：环境变量 > 配置文件
    if "EMAIL_AUTH_CODE" in os.environ:
        config.setdefault("email", {})["password"] = os.environ["EMAIL_AUTH_CODE"]
    if "WECHAT_WORK_WEBHOOK" in os.environ:
        config.setdefault("wechat_work", {})["webhook"] = os.environ["WECHAT_WORK_WEBHOOK"]
    if "SERVER_CHAN_SENDKEY" in os.environ:
        config.setdefault("server_chan", {})["sendkey"] = os.environ["SERVER_CHAN_SENDKEY"]
    if "DINGTALK_WEBHOOK" in os.environ:
        config.setdefault("dingtalk", {})["webhook"] = os.environ["DINGTALK_WEBHOOK"]

    return config


def get_notifier(config_path: str | None = None) -> INotifier:
    """
    获取全局通知器实例（线程安全的模块级单例）

    Args:
        config_path: 配置文件路径，None则用默认路径
    Returns:
        INotifier实例
    """
    global _notifier_instance

    if _notifier_instance is not None:
        return _notifier_instance

    with _notifier_lock:
        if _notifier_instance is not None:  # 双重检查
            return _notifier_instance

        if config_path is None:
            workspace = Path(__file__).parent.parent / "vnpy_workspace"
            config_path = str(workspace / "notify_config.json")

        config = _load_config(config_path)
        _notifier_instance = WebhookNotifier(config)
        return _notifier_instance


def set_notifier(notifier: INotifier) -> None:
    """
    替换全局实例（测试时用）
    回测时可以注入 NullNotifier
    """
    global _notifier_instance
    with _notifier_lock:
        _notifier_instance = notifier


def reset_notifier() -> None:
    """重置实例（测试用）"""
    global _notifier_instance
    with _notifier_lock:
        if _notifier_instance is not None:
            _notifier_instance.flush(timeout=5)
        _notifier_instance = None


# ============================================================
# 兼容旧代码的便捷函数
# ============================================================
def notify(message: str, level: str = "INFO"):
    lv = NotifyLevel(level) if isinstance(level, str) else level
    get_notifier().send(message, level=lv)


def notify_trade(strategy_name: str, trade_info: dict):
    get_notifier().send_trade(strategy_name, trade_info)


def notify_error(strategy_name: str, error: str, exception=None):
    get_notifier().send_error(strategy_name, error, exception)


# ============================================================
# 测试入口
# ============================================================
if __name__ == "__main__":
    logger.info("开始自测...")
    n = get_notifier()
    n.send("自测消息", level=NotifyLevel.INFO, force=True)
    n.flush(timeout=10)
    logger.info("自测完成")
