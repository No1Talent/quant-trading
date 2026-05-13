# Quant Patch — vn.py 量化交易补丁包

一组基于 **vn.py + CTP** 的国内期货量化交易增强补丁，提供线程安全的多渠道通知系统、事件驱动的告警监听器、可断点续传的数据导入，以及示例 CTA 策略。

> 这不是独立框架，而是覆盖到 vn.py 工作目录（通常是 `C:\Quant\`）的一组文件。

---

## 它解决了什么

vn.py 原生没有：
- 一套线程安全、防风暴、异步发送的告警通道（邮件/企业微信/钉钉/Server 酱）。
- 让策略代码**完全无需感知通知模块**的事件订阅机制。
- CSV 历史数据的事务化、断点续传导入。

本补丁补齐这三块，并附两个示例 CTA 策略可直接跑。

---

## 快速开始

```powershell
# 1. 覆盖到 vn.py 工作目录
xcopy /E /I Quant_patch\* C:\Quant\

# 2. 复制配置模板并填凭据
copy vnpy_workspace\connect_ctp.json.template    vnpy_workspace\connect_ctp.json
copy vnpy_workspace\notify_config.json.template  vnpy_workspace\notify_config.json

# 3. 启动
python vnpy_workspace\run.py
```

完整步骤、配置项说明、回测禁用通知等 → [docs/getting-started.md](docs/getting-started.md)。

---

## 文档导航

| 文档 | 用途 |
|------|------|
| [getting-started](docs/getting-started.md) | 新手安装、配置、启动 |
| [architecture](docs/architecture.md) | 架构图与事件驱动解耦原则 |
| [strategy-development](docs/strategy-development.md) | 如何编写自己的策略 |
| [operations](docs/operations.md) | 日志位置、告警渠道、回测禁通知 |
| [security](docs/security.md) | 凭据管理与 `.gitignore` 约定 |
| [data-import](docs/data-import.md) | `import_data.py` 用法 |
| [troubleshooting](docs/troubleshooting.md) | 常见错与 FAQ |
| [roadmap](docs/roadmap.md) | 未来优化方向（P0–P3） |
| [CHANGELOG](CHANGELOG.md) | 版本变更记录 |

---

## 核心约定（必读）

**策略文件不允许 `import utils.notifier`**。只用 `self.write_log(...)`，由挂在事件总线上的 `NotifyListener` 独立完成推送。

为什么？详见 [architecture.md](docs/architecture.md)。

---

## 项目结构

```
Quant_patch/
├── README.md / CHANGELOG.md
├── docs/                  # 所有文档
├── utils/                 # 通知核心 + 监听器 + 装饰器
├── strategies/            # 示例策略
├── vnpy_workspace/        # 入口 run.py + 配置模板
├── tests/                 # pytest 单元测试
├── import_data.py         # CSV → 数据库
└── logs/                  # 运行时日志（.gitignore）
```

---

## License & 联系

补丁内部使用，未对外发布。问题与建议请走 issue。
