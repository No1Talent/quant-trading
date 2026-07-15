# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号采用语义化版本（SemVer）。

---

## [Unreleased]

### Added
- **CTP 启动后侧对账（reconciler）** wired into `vnpy_workspace/run.py` — 启动后比对本地 `cta_strategy_data.json` 持仓与 CTP 实盘，不一致即 halt + `logs/reconcile_breach.flag` + CRITICAL 告警；下次启动 `check_reconcile_flag` 拦截直到人工删 flag。Init-Settle-Quiet 时序处理 CTP 1-QPS 限制。
- **`QUANT_MODE=SIGNAL_ONLY` 模式** — `make_signal_only_class(CtpGateway)` 工厂拦截 `send_order`，同步合成 ALLTRADED + Trade，给运营推"信号触发"提示。沙箱 cwd `vnpy_workspace/.signal_only_runtime/` 隔离 `cta_strategy_data.json`，假成交不污染 LIVE。20 个单测覆盖同步派发、tick storm、is_virtual 标记、watchdog。
- **`QUANT_MODE=REPLAY` 模式** — 新增 `utils/replay_gateway.py:ReplayGateway`，从 DB 回放历史 bar 合成 tick 走 SIGNAL_ONLY 同款合成路径。logical-clock 隔离让 RiskGuard 60s 窗口不被物理压缩误杀。tag `sit-replay-v1`。
- **飞书（Feishu）通知渠道** — `utils/notifier.py:_send_feishu` 实现群机器人 HMAC-SHA256 签名（`timestamp+"\n"+secret` 为 key，空消息签名 base64），支持 `<at user_id="all">` 全员标记。环境变量 `FEISHU_WEBHOOK` / `FEISHU_SECRET` 覆盖配置文件。5 个单测含官方算法对照。
- 共享合成原语 `synthesize_order_trade` / `dispatch_sync` / `notify_signal` / `OrderIdSequencer` from `utils/signal_only_gateway.py`（SIGNAL_ONLY 与 REPLAY 共用），`dispatch_sync` 内置 100ms watchdog 提示 handler 违反同步合约。
- `docs/operations.md` 新增 QUANT_MODE 章节。`docs/security.md` 环境变量表加入 `FEISHU_WEBHOOK` / `FEISHU_SECRET`。
- **研究知识库 in-repo 化**：新增 `docs/research-findings.md`（Layer ② master 结论 + 上线判级：vol-target AG-solo 为唯一可上线候选）与 `research/README.md`（h1→h7 / m0.5→m3.9 脚本导航 + 命名约定）。此前完整研究弧只存在于 auto-memory，仓库内无落点。
- `scripts/ctp_smoke.py`（原根目录散落的 `_ctp_smoke.py`）：headless SimNow 连通性冒烟测试，零下单，供上线前 preflight 复用。
- **P1 生命周期解耦（PR #6）**：`utils/rollover.py`、`ProductRegistry`（`config/products.yaml` 单一真源）、`SignalLog`、RiskGuard underlying 聚合限额。
- **Layer ② 研究弧收官（PR #8/#9/#15/#16/#17）**：截面因子 M0.5–M2.5（N=20 判负归档）；CPCV/PWF 去拟合把 H4 集成 WFA +0.993 压到 +0.526（不显著、kurt 63）；causal vol-target 翻案（组合 +0.782 显著，AG-solo +1.207）；Layer ③ 成本（5× 滑点全过）与资金（AG-solo ≥50 万 PASS）两道闸完成。结论真源 `docs/research-findings.md`。
- **ExitPolicy 离场纪律模块 `utils/exit_policy.py`（PR #10）**：固定/技术位/时间止损 + 固定/ATR 跟踪/保本止盈，全策略可复用。
- **剑客分时图 A/B/C 主信号策略 + tick 数据导入（PR #11）**：M4 在 1h rb 上证伪（`research/m4_vwap_findings.md`），策略保留为研究记录。
- **策略时间框架契约（PR #13、#14）**：`BaseCtaStrategy.bar_interval` / `live_eligible` 单一真源 + live 启动守卫，修"研究 1h vs 实盘 1min"漂移；vwap 策略定格 1m。
- **`strategies/vol_target_ma_strategy.py`（PR #18）**：M3.7 判级冠军（vol-target AG-solo）的 live 形态，实盘 pre-flight 清单在 docstring。
- **Market Intel 面板 `streamlit_market.py`**：单合约 + `config/market_watchlist.yaml` watchlist 卡片，`utils/market_intel.py` / `utils/market_data.py` 支撑。

### Fixed
- 飞书响应双 schema 检查 bug：`{"code":19021}` 错误（v2，无 StatusCode 字段）被 `not in (0, None) and ...` 条件吞掉。改为优先 `code`、回退 `StatusCode`、缺省视为成功。
- **signal_service 半合并事故（PR #21）**：修复 import 断裂并禁用两个 falsified 定时任务（与数据源实际能力不符的 AkShare 自动刷新）。

### Changed
- 删除冗余的"补丁/patch"叙述：本仓库即 vn.py 工作目录，不是覆盖到别处的文件集。
- 模板文件 (`connect_ctp.json.template` / `notify_config.json.template`) 改为纯占位符，移除内嵌的 `_说明` / `_警告` 注释字段。`notify_config.json.template` 加入 `feishu` 区段。
- 代码精简：移除横幅注释和未使用的 `notify()` / `notify_trade()` / `notify_error()` 便捷函数。
- README 渠道列表加入"飞书"。
- `.gitignore`：忽略 `research/*_log.txt` 运行日志（大体积、可再生）；白名单 4 个判级用 M 系列汇总 CSV（m36/m37/m38/m39），使上线证据链入库。
- `docs/research-findings-2026-05.md` 加"已被取代"横幅，标明其仅覆盖 60min 阶段。
- 依赖安全底线（PR #12）：scipy 声明为 `[research]` extra；requests≥2.32 / urllib3≥2.5 CVE floors；审查记录 `docs/maintenance-review-2026-06.md`。
- CI actions：checkout 4→6→7、setup-python 5→6（PR #1/#2/#20，Dependabot）。
- **陈旧清扫（2026-07-15）**：CHANGELOG 补记 PR #6–#21；roadmap 状态同步（P0-4 ✅ / P2-11 ◐ / P1-6 ⏸）；README 能力清单与结构树更新；watchlist 换 2026-07 主力合约并加入棕榈油 p2609；`config/products.yaml` 注册 P；`.gitignore` 研究白名单模式化（`*_summary.csv` / `*_stats.csv`）并入库 12 个 h*/m* 判级表；因子 WIP stash 归档为 `archive/factor-wip-stash`；清理已合并分支。

### Removed
- 删除陈旧生成产物 `quant_codebase_context.md`（gitignored，可由 `export_codebase.py` 再生）与 6 个研究 stdout 运行日志 `research/*_log.txt`（结论已沉淀到 docs + summary CSV）。

---

## [0.2.0]

### Fixed
- **SEVERE-1** 单例竞态：`Notifier.__new__` 双重检查锁存在时序窗口 → 改用模块级 `get_notifier()` + `threading.Lock`。
- **SEVERE-2** 线程池不关闭：进程退出时在途消息全丢 → `atexit` 注册 + 显式 `flush()`；`run.py` 关闭时主动等待。
- **SEVERE-3** 去重/限流容器多线程不安全 → 新增 `_dedup_lock` / `_rate_lock`，所有读写在锁内完成。
- **SEVERE-4** 凭据明文无保护 → 加入 `.gitignore`，模板文件改 `.template` 后缀，敏感字段支持环境变量覆盖。
- **SEVERE-5** 内部错误递归告警 → Notifier 内部错误只走 logging，监听器跳过 `[Notifier]` / `[NotifyListener]` 前缀。
- **SEVERE-6** Mixin 依赖 MRO → 废弃 `NotifyMixin`，改事件订阅式 `NotifyListener`，策略代码零依赖。

### Changed
- 策略与 Notifier 解耦：引入 `INotifier` 接口 + `NullNotifier`（回测注入用）。
- 网络调用稳健性：`requests.Session` + `HTTPAdapter(Retry)` 连接池+重试。
- 输出统一：以 `logging` 替代散落的 `print`，自动落盘到 `logs/notifier.log`。
- 关键接口补齐 PEP 484 类型注解。
- Tick 缓冲：`list.pop(0)` O(n) → `deque(maxlen=N)` O(1)。
- `WebhookNotifier`：渠道派发改为基于 `_CHANNEL_DEFS` 表驱动，新增渠道只需添加一行。
- `_check_rate_limit`：内部容器由 `list` 改为 `collections.deque`，过期窗口用 `popleft()` 摊还 O(1)。
- `_dispatch`：在 `__init__` 时缓存 `level_routing`，避免每条消息重新解析配置。
- 数据导入事务安全：[`import_data.py`](import_data.py) 分批写入 + 进度文件 + 断点续传。
- 文档体系重构：新增 README/CHANGELOG + `docs/` 主题分册。

### Added
- 引擎层风控前置 `utils/risk_guard.py`：日内回撤 / 持仓 / 频次熔断 + 落盘 `logs/risk_breach.flag`。
- 日志切割：`TimedRotatingFileHandler(when="midnight", backupCount=30)`，每日切割保留 30 天。
- `tests/test_notifier.py`：pytest 覆盖单例并发、去重/限流并发、flush 行为、渠道失败隔离。
- `tests/test_risk_guard.py`：覆盖各熔断条件、cancel 调用、breach flag 落盘、并发安全。
- CI：GitHub Actions + pre-commit (ruff / ruff-format / mypy / gitleaks / pytest-fast) + Dependabot。
- `vnpy_workspace/*.template`：可入库的配置模板（占位符版）。

### Removed
- `utils/strategy_base.NotifyMixin`：仅保留 `safe_callback` 装饰器。
