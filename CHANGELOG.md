# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号采用语义化版本（SemVer）。

---

## [Unreleased]

### Changed
- `WebhookNotifier`：渠道派发改为基于 `_CHANNEL_DEFS` 表驱动，新增渠道只需添加一行。
- `_check_rate_limit`：内部容器由 `list` 改为 `collections.deque`，过期窗口用 `popleft()` 摊还 O(1)。
- `_dispatch`：在 `__init__` 时缓存 `level_routing`，避免每条消息重新解析配置。
- 文档体系重构：新增 README/CHANGELOG + `docs/` 主题分册。

---

## [0.2.0] — v2 修复版

修复了 v1 在生产环境暴露的 6 个严重缺陷与 6 项优化，明细如下。

### Fixed (SEVERE)
- **SEVERE-1** 单例竞态：`Notifier.__new__` 双重检查锁存在时序窗口 → 改用模块级 `get_notifier()` + `threading.Lock`。
- **SEVERE-2** 线程池不关闭：进程退出时在途消息全丢 → `atexit` 注册 + 显式 `flush()`；`run.py` 关闭时主动等待。
- **SEVERE-3** 去重/限流容器多线程不安全 → 新增 `_dedup_lock` / `_rate_lock`，所有读写在锁内完成。
- **SEVERE-4** 凭据明文无保护 → 加入 `.gitignore`，模板文件改 `.template` 后缀，敏感字段支持环境变量覆盖。
- **SEVERE-5** 内部错误递归告警 → Notifier 内部错误只走 logging，监听器跳过 `[Notifier]` / `[NotifyListener]` 前缀。
- **SEVERE-6** Mixin 依赖 MRO → 废弃 `NotifyMixin`，改事件订阅式 `NotifyListener`，策略代码零依赖。

### Changed (OPT)
- **OPT-1** 策略与 Notifier 解耦：引入 `INotifier` 接口 + `NullNotifier`（回测注入用）。
- **OPT-3** 网络调用稳健性：`requests.Session` + `HTTPAdapter(Retry)` 连接池+重试。
- **OPT-4** 输出统一：以 `logging` 替代散落的 `print`，自动落盘到 `logs/notifier.log`。
- **OPT-7** 类型注解：关键接口补齐 PEP 484 注解。
- **OPT-8** Tick 缓冲：`list.pop(0)` O(n) → `deque(maxlen=N)` O(1)。
- **DB-1** 数据导入事务安全：[`import_data.py`](import_data.py) 分批写入 + 进度文件 + 断点续传。

### Added
- `tests/test_notifier.py`：pytest 覆盖单例并发、去重/限流并发、flush 行为、渠道失败隔离。
- `vnpy_workspace/*.template`：可入库的配置模板。

### Removed
- `utils/strategy_base.NotifyMixin`：仅保留 `safe_callback` 装饰器。

---

## [0.1.0] — 初版

最初的 vn.py 量化补丁雏形（已 deprecated，仅作历史记录）。
