# 安全与凭据管理

涉及 CTP 账号密码、邮箱授权码、Webhook 私钥等敏感字段——配置失误会被 git 把密码推到远端。这里是规范。

---

## 一、永远不要提交的文件

`.gitignore` 已经排除：

```gitignore
**/connect_ctp.json       # CTP 账号密码
**/notify_config.json     # 邮箱授权码、Webhook、SendKey

# 但保留模板
!**/connect_ctp.json.template
!**/notify_config.json.template
```

`*.template` 入库，真实凭据**永远不入 git**。

> ⚠️ **不要 `git add -f connect_ctp.json`**——`-f` 会绕过 `.gitignore`，密码就上远端了。

---

## 二、敏感字段优先级

加载顺序（高优先级覆盖低优先级）：

```
环境变量  >  notify_config.json  >  默认值（无）
```

| 环境变量 | 覆盖字段 |
|---------|---------|
| `EMAIL_AUTH_CODE` | `config["email"]["password"]` |
| `WECHAT_WORK_WEBHOOK` | `config["wechat_work"]["webhook"]` |
| `SERVER_CHAN_SENDKEY` | `config["server_chan"]["sendkey"]` |
| `DINGTALK_WEBHOOK` | `config["dingtalk"]["webhook"]` |

实现见 [`_load_config`](../utils/notifier.py#L411)。

---

## 三、生产环境推荐做法

**不要**把真实密码写进 `notify_config.json`。`notify_config.json` 里只填非敏感字段（`enabled`、`server`、`port`、`username`、`sender`、`receiver`）；敏感字段全走环境变量：

### Windows（PowerShell 持久化）

```powershell
[System.Environment]::SetEnvironmentVariable("EMAIL_AUTH_CODE", "xxx", "User")
[System.Environment]::SetEnvironmentVariable("WECHAT_WORK_WEBHOOK", "https://...", "User")
```

设置后**新开**一个 PowerShell 窗口才生效。

### Windows（仅当前会话）

```powershell
$env:EMAIL_AUTH_CODE = "xxx"
python vnpy_workspace\run.py
```

### 使用 `.env` 文件（开发用，已 gitignore）

```
# .env
EMAIL_AUTH_CODE=xxx
WECHAT_WORK_WEBHOOK=https://...
```

启动前用 `python-dotenv` 加载（需要自己加），或者写个批处理：
```batch
@echo off
for /f "tokens=*" %%i in (.env) do set %%i
python vnpy_workspace\run.py
```

---

## 四、CTP 密码呢？

目前 `connect_ctp.json` **没有**环境变量覆盖支持——CTP 凭据由 vn.py 主引擎直接读文件，不经过我们的 `_load_config`。

短期方案：保护好 `connect_ctp.json` 的文件权限（Windows 上右键属性→安全→只给当前用户读权限）。

长期方案：用 Windows Credential Manager 或 HashiCorp Vault，详见 [roadmap.md](roadmap.md) P0 第 1 条。

---

## 五、Webhook URL 也是密码

企业微信群机器人、钉钉、Server 酱的 webhook URL **本质上是密码**——任何拿到 URL 的人都可以往你的群发消息。

- 不要在截图、Slack、issue 里贴完整 URL。
- 不要硬编码进任何 `.py` 文件。
- 走 `.gitignore` 文件或环境变量。

---

## 六、检查清单（每次发版前）

```powershell
git status
git diff --cached
```

确认：
- [ ] 没有 `connect_ctp.json` / `notify_config.json` 出现在变更列表。
- [ ] 没有 `*.log` 文件被加入暂存区。
- [ ] 没有 `__pycache__`、`.mypy_cache` 被加入暂存区。
- [ ] 没有任何形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx` 的硬编码 URL。

可以用 `git diff --cached | grep -iE "password|webhook|sendkey|token"` 做最后一道 grep 防线。

---

## 七、已泄露怎么办

如果不小心把凭据 commit + push 了：

1. **立刻**到对应平台撤销/重置该凭据（QQ 邮箱授权码、企业微信机器人 key、SimNow 密码）。
2. 从 git 历史移除：
   ```powershell
   git filter-repo --invert-paths --path notify_config.json
   git push --force-with-lease origin main
   ```
   （需要 `git-filter-repo`，比 `filter-branch` 安全）
3. 通知所有 clone 过仓库的人重新 clone（否则他们本地仍有泄露副本）。
4. 反思流程：怎么发生的？需不需要加 pre-commit hook 阻止？
