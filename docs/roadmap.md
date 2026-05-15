# 未来优化方向

按"该做的紧迫程度"排序。P0 = 应尽快做，P3 = 锦上添花。

每一项给出**问题**、**当前状态**、**建议做法**、**影响文件**，方便认领。

---

## P0 — 生产稳定性

### P0-1 凭据管理升级到系统密钥库

**问题**：环境变量是过渡方案。Webhook URL、邮箱授权码长期放在用户环境变量里，被恶意进程读到的风险仍在；CTP 密码目前还在 `connect_ctp.json` 明文。

**当前状态**：[`_load_config`](../utils/notifier.py) 已支持环境变量覆盖；CTP 凭据由 vn.py 直接读文件，无覆盖机制。

**建议做法**：
- 抽象一个 `CredentialProvider` 接口，实现 `EnvProvider` / `KeyringProvider` / `VaultProvider` 三种。
- Windows 用 [`keyring`](https://pypi.org/project/keyring/) 走 Credential Manager，跨平台兼容。
- CTP 凭据要 patch vn.py 加载逻辑（或写个外壳脚本启动前注入）。

**影响文件**：`utils/notifier.py`、新增 `utils/credentials.py`、可能要 patch `vnpy_workspace/run.py`。

---

### P0-2 风控前置（最大回撤 / 单笔上限 / 日内熔断） ✅ 已完成

**问题**：`run.py` 加载了 `RiskManagerApp`，但策略层**没有**任何自我熔断。策略 bug 或市场异常时无止损兜底。

**当前状态**：已落地。`utils/risk_guard.py` 订阅 `EVENT_TRADE` / `EVENT_ACCOUNT`，三条规则触发任一即撤单 + CRITICAL + 落盘 `logs/risk_breach.flag`。**不做自动平仓**——保留给人工。`run.py` 已挂载并在启动时调 `check_breach_flag()`。运维细节看 [operations.md](operations.md) 的"风控熔断"一节，测试看 [`tests/test_risk_guard.py`](../tests/test_risk_guard.py)。

**影响文件**：新增 `utils/risk_guard.py` + `tests/test_risk_guard.py`，`vnpy_workspace/run.py` 挂载，`utils/__init__.py` 导出。

---

### P0-3 日志切割 ✅ 已完成

**问题**：`logs/trader.log` 和 `logs/notifier.log` 都用 `FileHandler`——永远不滚动，几个月后磁盘占满。

**当前状态**：已落地。两个文件都换成 `TimedRotatingFileHandler(when="midnight", backupCount=30, delay=True)`——每天 0 点切割，保留 30 天，首次写入才打开文件句柄。

**影响文件**：`utils/notifier.py`、`vnpy_workspace/run.py`。

---

### P0-4 策略层回归测试

**问题**：`tests/test_notifier.py` 只覆盖通知器。策略代码改一行可能默默引入信号逻辑 bug，回测 OK 实盘炸。

**当前状态**：策略零测试覆盖。

**建议做法**：
- 把每个策略的纯计算逻辑（信号判断、止损止盈）抽到独立函数。
- 用固定数据集（fixtures CSV）跑 `vnpy_ctabacktester`，断言最终 PnL / 持仓 / 交易次数。
- CI 上跑：参数改动 → 测试断言失败 → 改不动。

**影响文件**：`tests/test_strategies.py` 新增，可能要重构 `strategies/*.py` 把计算逻辑抽出来。

---

## P1 — 中期改进

### P1-5 配置 schema 化（pydantic）

**问题**：`notify_config.json` 字段拼错（如 `dedup_windows_second` 多个 s）启动不报错，发不出消息时才发现。

**建议做法**：
```python
from pydantic import BaseModel

class EmailConfig(BaseModel):
    enabled: bool = False
    server: str
    port: int = 465
    # ...

class NotifyConfig(BaseModel):
    dedup_window_seconds: int = 60
    rate_limit_per_minute: int = 30
    email: EmailConfig | None = None
    # ...
```

`_load_config` 直接 `NotifyConfig.model_validate(json_dict)`，拼错就 raise。

**影响文件**：`utils/notifier.py`、`pyproject.toml` 加 `pydantic` 依赖。

---

### P1-6 可观测性（Prometheus / Grafana）

**问题**：没法回答"上小时发了多少通知？哪个渠道最慢？哪个被限流最多？"

**建议做法**：
- 用 `prometheus_client` 暴露 `/metrics` HTTP endpoint。
- 指标：
  - `notifier_messages_sent_total{channel, level}` — counter
  - `notifier_messages_dropped_total{reason}` — counter（reason: rate_limit / dedup / shutdown）
  - `notifier_send_duration_seconds{channel}` — histogram
  - `notifier_executor_queue_size` — gauge
- Grafana 看板 + 告警规则（"5 分钟丢弃 > 50 条"）。

**影响文件**：`utils/notifier.py`、新增 `utils/metrics.py`、Grafana JSON 模板入库 `docs/observability/`。

---

### P1-7 多账户支持

**问题**：`NotifyListener.last_balance` 是单字段（[notify_listener.py:103](../utils/notify_listener.py#L103)），两个 CTP 账户会互相覆盖。

**建议做法**：
```python
self.last_balance: dict[str, float] = {}  # accountid -> balance
```
`on_account` 按 `account.accountid` 区分。告警消息里也要带账户 ID。

**影响文件**：`utils/notify_listener.py`。

---

### P1-8 策略级告警过滤

**问题**：当前所有策略共享一个 listener，无法按策略关停某些告警。

**建议做法**：
- `notify_config.json` 加 `strategy_filters` 字段：
  ```json
  "strategy_filters": {
      "MyHFTStrategy": {"mute_levels": ["INFO"]},
      "*": {"mute_keywords": ["心跳"]}
  }
  ```
- `NotifyListener` 在各 handler 里检查策略名 + level，命中规则就跳过。

**影响文件**：`utils/notify_listener.py`、配置 schema、文档。

---

### P1-9 CTP 断线自愈

**问题**：断线只推 CRITICAL 告警，没自动重连退避。人不在线就一直断着。

**建议做法**：
- 新增 `utils/gateway_watcher.py`，订阅 `EVENT_LOG`。
- 命中"断线"关键词 → 按指数退避（10s, 30s, 60s, 120s）调 `main_engine.connect("CTP", config)`。
- 最多重试 N 次后放弃，再推一条 CRITICAL。
- 重连成功推 INFO，恢复正常。

**影响文件**：新增 `utils/gateway_watcher.py`、`vnpy_workspace/run.py` 挂载。

---

## P2 — 长期/锦上添花

### P2-10 异步化（asyncio）

把 `ThreadPoolExecutor` 换成 `asyncio` + `aiohttp` + `aiosmtplib`。所有渠道都是 I/O 密集，单事件循环吞吐比 4 线程池更高，shutdown 也更干净。

**影响**：API 兼容性。`send()` 改成 `async def` 后，调用方都要 await。可以维持同步外壳，内部 `asyncio.run_coroutine_threadsafe`。

工作量大，收益是性能 + 干净。优先级 P2。

---

### P2-11 回测/实盘契约测试

写 contract test 验证：同一份 K 线序列、同一份策略、同一份参数 → 回测引擎和实盘事件流产出的 `buy/sell/cover/short` 调用顺序与价格完全一致。

发现差异就报——回测能赚不代表实盘能赚，常见因素：tick 数据精度、order 撮合假设、滑点模型。

---

### P2-12 数据导入并行化

[`import_data.py`](../import_data.py) 现在单线程。多合约导入可以按 `(symbol, exchange)` 并行。

注意：
- vnpy 用 SQLite 时**不支持并发写**（数据库锁）。
- MySQL/PostgreSQL 后端可以并行。
- 检查 `database` 对象的线程安全性。

---

### P2-13 策略参数版本化

每次策略启动把 `parameters` 落库（一张 `strategy_runs` 表），配合成交流水做 attribution——"3 月那段亏损是 fast_window=10 时跑的，4 月调成 15 后好了"。

---

### P2-14 告警路由的灰度

`level_routing` 现在是静态映射。扩展按时段、按策略路由：
```json
"routing_rules": [
    {"when": "time >= 21:00", "level": "INFO", "channels": ["dingtalk"]},
    {"when": "time < 21:00",  "level": "INFO", "channels": ["wechat_work"]}
]
```

---

## P3 — 架构层面

### P3-15 抽象交易所/经纪商

当前与 CTP 强绑定。未来接股票（XTP）、加密（CCXT）需要把 `run.py` 的 Gateway 加载抽成插件式：

```python
gateways = load_gateway_plugins(config)
for gw in gateways:
    main_engine.add_gateway(gw.cls)
```

---

### P3-16 从 vn.py 解耦

`NotifyListener` 依赖 `vnpy.trader.event` 的 5 个事件名。引入薄薄的 `EventProtocol`，让通知模块在非 vnpy 场景（如自研撮合引擎）也能复用。

---

## 怎么认领

- 选一个 P0/P1 项。
- 在 issue 里 @负责人，描述设计意图。
- 写代码 + 测试 + 文档。
- PR 引用 issue 编号，标题写 `feat(P0-2): risk_guard 初版`。

如果发现这份 roadmap 漏了什么——开 issue 加进来。
