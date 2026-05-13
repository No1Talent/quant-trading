# 运维手册

日志、告警渠道、回测、关闭流程的所有运维细节。

---

## 日志

| 文件 | 写入方 | 用途 |
|------|--------|------|
| `logs/trader.log` | `run.py` 的 root logger | 主进程启动/关闭流水 |
| `logs/notifier.log` | `notifier.py` 的 `logging.getLogger("notifier")` | 通知器内部状态、各渠道发送结果、失败原因 |

两个文件目前都用 `FileHandler`——**不会自动滚动**。生产环境建议改 `TimedRotatingFileHandler`，见 [roadmap.md](roadmap.md) 的 P0 第 3 条。

### 实时跟踪

```powershell
Get-Content C:\Quant\logs\notifier.log -Wait -Tail 50
```

### 日志级别

`run.py` 的 root logger 默认 `INFO`。要调成 DEBUG 临时排查时：

```python
logging.basicConfig(level=logging.DEBUG, ...)
```

注意 DEBUG 会让 vn.py 的事件大量打印，磁盘吃得快。

---

## 告警渠道分级路由

`notify_config.json` 里的 `level_routing` 字段决定哪些级别走哪些渠道：

```json
"level_routing": {
    "INFO":     ["wechat_work"],
    "WARNING":  ["all"],
    "ERROR":    ["all"],
    "CRITICAL": ["all"]
}
```

- `"all"` 表示所有 `enabled: true` 的渠道。
- 也可以列具体渠道：`["email", "dingtalk"]`。
- 未列出的级别走默认 `["all"]`。

典型策略：
- INFO：信息量大，只走一个轻量渠道（微信群机器人）。
- WARNING/ERROR/CRITICAL：所有渠道都发，宁可重复也不要漏。

---

## 去重与限流

| 参数 | 默认 | 含义 |
|------|------|------|
| `dedup_window_seconds` | 60 | 同一条消息 N 秒内只发一次 |
| `rate_limit_per_minute` | 30 | 每分钟最多发 N 条 |

`force=True` 会跳过去重和限流。所有"告警类"消息（`send_warning` / `send_error` / `send_critical` / `send_daily_report`）默认 `force=True`，确保关键告警不被吞掉。

---

## 关闭流程（重要）

`run.py` 在 `finally` 块里调用 `notifier.flush(timeout=10)`：

```python
finally:
    notifier.send("交易系统已关闭", force=True)
    notifier.flush(timeout=10)   # 等所有在途消息发完
    main_engine.close()
```

这是 SEVERE-2 修复的核心：**没有 `flush()` 时，进程退出会丢掉线程池里没发完的消息**。如果你自己写新的入口脚本，**必须**保留这段 `finally`。

---

## 回测时禁用通知

两种等价做法：

### 方法 1：不挂监听器（推荐）

回测脚本通常不调用 `attach_notify_listener(...)`，事件总线上没有订阅者，自然没有推送。

### 方法 2：注入 NullNotifier

如果你的策略代码里**意外**调用了 `get_notifier()`（不应该，但兜底）：

```python
from utils.notifier import NullNotifier, set_notifier
set_notifier(NullNotifier())   # 回测脚本最顶上调一次
```

`NullNotifier` 是 `INotifier` 接口的零副作用实现。

---

## 多账户

当前 `NotifyListener.last_balance` 是单字段，**只支持一个账户**（[notify_listener.py:103](../utils/notify_listener.py#L103)）。

如果接两个 CTP 账户：
- 现状：账户余额监控会互相覆盖，可能漏告警。
- 临时解决：把 `last_balance` 改成 `dict[accountid, float]`。
- 长期：见 [roadmap.md](roadmap.md) P1 第 7 条。

---

## 监控自身

目前没有指标导出。建议生产环境观察的指标：
- 已发通知数（按渠道）
- 去重命中次数
- 限流丢弃次数
- 各渠道平均延迟与失败率

实现思路见 [roadmap.md](roadmap.md) P1 第 6 条（Prometheus）。

---

## 常见运维场景

### 修改通知渠道凭据
直接编辑 `notify_config.json`，**重启进程**才生效。运行时热加载未实现。

### 暂时禁用某个渠道
把 `"enabled"` 改 false，重启。

### 临时升级日志级别
编辑 `run.py` 的 `logging.basicConfig(level=...)`，重启。

### 配置写错了导致启动报错
看 `logs/trader.log` 里的 traceback。常见问题汇总在 [troubleshooting.md](troubleshooting.md)。
