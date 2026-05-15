# 运维手册

日志、告警渠道、回测、关闭流程的所有运维细节。

---

## 日志

| 文件 | 写入方 | 用途 |
|------|--------|------|
| `logs/trader.log` | `run.py` 的 root logger | 主进程启动/关闭流水 |
| `logs/notifier.log` | `notifier.py` 的 `logging.getLogger("notifier")` | 通知器内部状态、各渠道发送结果、失败原因 |
| `logs/risk_breach.flag` | `utils/risk_guard.py`（仅熔断时） | 风控熔断标志，下次启动 `run.py` 会读取并告警 |

两个日志文件都用 `TimedRotatingFileHandler(when="midnight", backupCount=30)`——**每日切割、保留 30 天**。无需手动清理。

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

## 风控熔断（P0-2）

`run.py` 启动后挂载 [`utils.risk_guard.RiskGuard`](../utils/risk_guard.py)，订阅 `EVENT_TRADE` / `EVENT_ACCOUNT`，触发任一条即立刻撤单 + CRITICAL 告警。

### 默认阈值

| 字段 | 默认 | 含义 |
|------|------|------|
| `max_daily_loss_pct` | 0.05 | 日内回撤超过初始余额 5% → 熔断 |
| `max_position_per_symbol` | 10 | 单合约绝对净持仓 > 10 手 → 熔断 |
| `max_trades_per_minute` | 20 | 60 秒内成交超过 20 笔 → 熔断 |

调整阈值：编辑 `vnpy_workspace/run.py` 里 `attach_risk_guard(...)` 的 kwargs。

### 熔断动作

1. `main_engine.cancel_all_active_orders()` 撤掉所有挂单。
2. 推 `CRITICAL` 告警（force=True，不去重不限流）。
3. 落盘 `logs/risk_breach.flag`（JSON，含触发时间和原因）。
4. 后续事件**不会**再次触发熔断（一次性 latch）。

**自动平仓不会做**——出于道德/合规考量，开仓决策让人工介入。

### 恢复流程

1. 看告警内容（推到所有 CRITICAL 渠道）。
2. 登录 SimNow/实盘账户检查持仓与挂单。
3. 决定是平仓、加保证金还是停止策略。
4. 删除标志文件：`del logs\risk_breach.flag`。
5. 重启 `run.py`。

> 如果不删 `risk_breach.flag`，`run.py` 启动时只会日志告警**不会强制退出**——这是为了避免周末/换月凌晨重启被卡死。但日志里会留 `CRITICAL` 记录，每次启动都看得到。

测试覆盖见 [`tests/test_risk_guard.py`](../tests/test_risk_guard.py)。

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

**方法 1（推荐）**：回测脚本不调用 `attach_notify_listener(...)`，事件总线上没有订阅者，自然没有推送。

**方法 2（兜底）**：如果策略代码意外调用了 `get_notifier()`，在脚本顶部注入空实现：

```python
from utils.notifier import NullNotifier, set_notifier
set_notifier(NullNotifier())
```

完整的回测规则（不挂 RiskGuard、数据路径、参数固化等）见 [development.md §4](development.md#4-回测规范)。

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
