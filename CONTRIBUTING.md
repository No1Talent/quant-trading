# 贡献与开发约定

本文档面向参与本补丁开发的人员。生产用户请看 [README.md](README.md)。

---

## 一、铁律（违反会引入历史已修复的 bug）

### 1. 策略代码不得 import 通知模块
```python
# ❌ 错
from utils.notifier import get_notifier
self.notifier = get_notifier()

# ✅ 对
self.write_log("信号: 金叉做多")   # 监听器自动推送
```
理由见 [docs/architecture.md](docs/architecture.md)，历史教训见 SEVERE-6（[changelog](docs/changelog/v2-severe-fixes.md#severe-6)）。

### 2. 通知器内部错误绝不能产生 vn.py 事件
任何 `_send_xxx` 失败 → **只**走 `logger.error(...)`。
违反此条会触发 SEVERE-5 描述的递归告警。

### 3. 共享可变状态必须加锁
`recent_messages` / `send_timestamps` 这类多线程访问的容器必须在 `_dedup_lock` / `_rate_lock` 内读写。
新增类似容器时，先想好它的锁。

### 4. 不得提交真实凭据
- `connect_ctp.json`、`notify_config.json` 已在 `.gitignore`，**永远不要 `git add -f`**。
- 真实凭据走环境变量（`EMAIL_AUTH_CODE` 等），见 [docs/security.md](docs/security.md)。

---

## 二、代码风格

- 格式化与 lint：`ruff` 已在 `pyproject.toml` 配好，行长 100。
- 类型检查：`mypy`，关键接口必须有 PEP 484 注解。
- 注释：**默认不写**。只在"读者一眼看不出为什么"时写一行 `# Why: ...`。代码"做什么"应该靠命名而非注释表达。
- 测试：所有线程相关代码必须有并发测试（参考 `tests/test_notifier.py` 的 `TestSingleton.test_concurrent_get_notifier_thread_safe`）。

---

## 三、提交规范

采用 Conventional Commits：

```
<type>: <subject>

<body>
```

`type` 取值：
- `feat:` 新功能
- `fix:` bug 修复
- `refactor:` 不改变外部行为的重构
- `docs:` 仅文档
- `test:` 仅测试
- `chore:` 构建/依赖/配置

`subject` 中文/英文均可，简洁明确。提交前跑：
```powershell
pre-commit run --all-files
pytest tests/ -v
```

---

## 四、新增渠道的步骤

以新增"飞书"为例：

1. 在 `WebhookNotifier._CHANNEL_DEFS` 添一行：
   ```python
   ("feishu", "飞书"),
   ```
2. 实现 `_send_feishu(self, title: str, message: str)`。所有 sender 签名必须是 `(title, message)`，不需要 title 的渠道用 `del title` 显式声明。
3. 在 `notify_config.json.template` 中加入 `"feishu": {"enabled": false, "webhook": "..."}` 字段，并在文档里补说明。
4. 在 `tests/test_notifier.py` 添一个 `test_feishu_called_when_enabled`。
5. `CHANGELOG.md` 的 `[Unreleased]` 加 `### Added`。

无需改 `_dispatch` 和 `_get_enabled_channels`——它们已是表驱动。

---

## 五、添加新事件订阅

在 `NotifyListener` 增加监听某个 vn.py 事件：

1. `__init__` 里 `event_engine.register(EVENT_XXX, self.on_xxx)`。
2. 实现 `on_xxx(self, event: Event)`，**避免在 handler 里发出会被自己监听到的 LOG 事件**（SEVERE-5）。
3. 在 `unregister()` 里加对应的 `unregister`，否则单元测试无法干净重启。

---

## 六、Roadmap 与 Issue

未来计划见 [docs/roadmap.md](docs/roadmap.md)。新想法先开 issue 讨论再动手——尤其是涉及通知器架构或 vn.py 事件的改动。
