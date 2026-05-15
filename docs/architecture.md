# 架构与设计原则

理解这一页，你就能避开历史上踩过的全部坑。

---

## 一张图

```
┌──────────────┐                                       ┌─────────────────┐
│   策略代码   │   self.write_log("信号: 金叉做多")    │  vn.py 事件总线 │
│ (CtaTemplate)│ ─────────────────────────────────▶   │  (EventEngine)  │
└──────────────┘                                       └────────┬────────┘
                                                                │
                              ┌─────────────────────────────────┴────┐
                              │                                      │
                              ▼                                      ▼
                    ┌──────────────────┐                  ┌──────────────────┐
                    │  vn.py 主引擎    │                  │  NotifyListener  │
                    │ (订单/成交/账户) │                  │  (本仓库新增)    │
                    └──────────────────┘                  └────────┬─────────┘
                                                                   │
                          ┌────────────┬──────────────┬────────────┴───────┐
                          ▼            ▼              ▼                    ▼
                    ┌──────────┐ ┌──────────┐ ┌─────────────┐ ┌──────────────┐
                    │  邮件    │ │ 企业微信 │ │  Server酱   │ │    钉钉      │
                    └──────────┘ └──────────┘ └─────────────┘ └──────────────┘
```

---

## 核心原则：策略 ⊥ 通知

**策略代码完全不感知通知系统的存在。**

策略只做一件事：把"发生了什么"写进 `write_log()`。这会触发 vn.py 的 `EVENT_LOG` 事件。挂在事件总线上的 [`NotifyListener`](../utils/notify_listener.py) 负责把这些事件转成各渠道的推送。

### 为什么必须这样？

旧版本（v1）用 `NotifyMixin` 让策略继承通知能力：

```python
# v1 的写法（已废弃）
class MyStrategy(NotifyMixin, CtaTemplate):
    def on_start(self):
        super().on_start()              # ← 漏掉就全失效
        self.notifier.send("启动")
```

问题（参见 CHANGELOG SEVERE-6）：
1. 依赖 MRO，子类重写 `on_start` 忘记 `super()` 整个机制就废了。
2. 回测时也会触发真实推送，开发期把测试群刷屏。
3. 策略代码与基础设施紧耦合，无法独立单元测试。

v2 改成事件订阅后：
- 策略文件**零依赖**通知模块。
- 回测时不挂 `NotifyListener` 就零副作用。
- 通知逻辑变更不会影响策略文件。

---

## NotifyListener 监听了哪些事件

| 事件 | 来源 | 触发动作 |
|------|------|---------|
| `EVENT_LOG` | 任何模块调用 `write_log()` | 关键词匹配，"断线/失败/Error" → 推 WARNING/CRITICAL |
| `EVENT_CTA_LOG` | CTA 策略日志 | 同上 |
| `EVENT_ORDER` | vn.py 订单状态变更 | `status==REJECTED` → 推拒单告警 |
| `EVENT_TRADE` | 成交回报 | 推成交明细 |
| `EVENT_ACCOUNT` | 账户资金推送 | 余额变化超过 `balance_alarm_pct`（默认 5%）推告警 |
| `EVENT_CTA_STRATEGY` | 策略状态变化 | 已初始化 → 运行中 推启动消息；反之推停止 |

---

## Notifier 内部结构

```
┌──────────────────────────────────────────────────────┐
│ get_notifier()  ← 模块级 + threading.Lock 单例       │
│   │                                                  │
│   ▼                                                  │
│ WebhookNotifier(INotifier)                           │
│   ├─ ThreadPoolExecutor(4)   ← 异步发送             │
│   ├─ deque[float]            ← 限流时间戳            │
│   ├─ dict[hash, ts]          ← 去重缓存              │
│   ├─ _level_routing (缓存)   ← INFO/WARN 路由       │
│   ├─ _CHANNEL_DEFS (表)      ← 4 渠道按顺序派发     │
│   └─ requests.Session        ← 连接池 + 重试        │
└──────────────────────────────────────────────────────┘
```

**关键不变量：**
- 所有读写 `recent_messages`/`send_timestamps` 必须在对应锁内。
- `_send_xxx` 失败信息**只走 `logger`**，绝不调用任何会产生 vn.py 事件的接口。
- `atexit` 注册的 `_shutdown_handler` 保证进程退出前 flush 在途消息。

---

## 派发流程

```
send(msg, force=False)
  │
  ├─[shutdown?]──▶ 丢弃
  ├─[rate limit?]─▶ 丢弃     (force=True 跳过)
  ├─[duplicate?]──▶ 丢弃     (force=True 跳过)
  │
  └─▶ executor.submit(_dispatch)
        │
        └─▶ 遍历 _CHANNEL_DEFS：
              对每个 (key, label)：
              若 level_routing[level] 含 "all" 或 key
              且 config[key].enabled
              则 _safe_call(_send_<key>, title, message)
```

---

## 回测模式

**不挂 NotifyListener** 即可：
```python
# 回测脚本里不调用 attach_notify_listener(...)
```

或者主动注入空实现：
```python
from utils.notifier import NullNotifier, set_notifier
set_notifier(NullNotifier())
```

`NullNotifier` 实现 `INotifier` 接口，所有方法都是 no-op。这样即使有代码不小心调用了 `get_notifier()`，也不会推送。

---

## 何时该加锁？

- **多线程会同时读写的容器** → 必须加。
- **只在构造时设置，运行时只读的字段**（如 `self.config`、`self._CHANNEL_DEFS`）→ 不需要。
- **executor.submit 是线程安全的**，所以提交动作本身不需要锁。但 `_shutdown_flag` 的检查+提交是 check-then-act，所以用 `_shutdown_lock` 保护。

---

## 相关文档

- [strategy-development.md](strategy-development.md) — 在策略里如何正确使用 `write_log` 与 `@safe_callback`
- [operations.md](operations.md) — 告警分级路由、去重限流、风控熔断的运维细节
- [security.md](security.md) — 通知渠道凭据的安全管理
