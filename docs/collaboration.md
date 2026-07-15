# 协作章程（Collaboration Charter）

我们（Elenka + Gemini + Claude + 仓库本身）如何分工、如何循环推进、如何在
跨 session 之间以最小成本无损地交接状态。这是**流程的 single source of truth**；
Claude 每个 session 执行的精简版规则见 auto-memory `feedback-handoff-protocol`。

配套阅读：[roadmap.md](roadmap.md)（做什么）、[development.md](development.md)
（怎么写代码）、[research-findings.md](research-findings.md)（研究结论的真源）。

---

## 1. 谁负责哪一块

一句话原则：**Elenka 拍板一切不可逆的决定；Claude 负责一切可复现的执行；
Gemini 提供外部视角；仓库承载真相。**

| 环节 | Accountable（拍板） | Executes（执行） | Reviews（把关） |
|------|------|------|------|
| 方向与优先级 | **Elenka** | Elenka | — |
| **数据** 选型 + 花费 | **Elenka**（花费） | Claude（评估 / 导入 / 质量） | — |
| **研究** 假设 | Claude | Claude（设计 + 跑） | Gemini（统计陷阱） |
| 研究 **结论**（promote/drop） | **Elenka** | Claude 提议 + deflate | Gemini sanity-check |
| **策略** 代码（研究→实盘形态） | Claude | Claude | Gemini + CI |
| **实盘晋级** / 合并 live 代码 | **Elenka**（红线） | Claude 备 pre-flight，Elenka 合并 | CI gate |
| 工程 / 基建（风控、通知、SIT） | Claude | Claude | CI |
| 方法论 & 统计护栏 | **Gemini** | — | Claude 落地 |
| **知识持久化 & 交接** | Claude | Claude | 自审（wrap-up 时） |

对既有 Trinity（[feedback-trinity-collaboration]）的唯一新增：**"知识持久化 &
交接"是一个有主的、点名的职责，且属于 Claude。** 2026-07-15 的漂移事故
（见 §5）正是因为"保持磁盘状态为真"从来不是谁的明确职责，只是 `/wrap-up-session`
的副产品。设成职责，意味着我每个 session 都对账，而不是想起来才对账。

### 三个 agent 是无状态的，仓库不是

PM / Architect / Lead-dev 三方每次都是冷启动。**仓库（git + 磁盘 + auto-memory）
是这场协作的第四个参与者**，也是唯一有状态的一个。因此每一个 commit、每一条
memory、每一份 in-repo doc 都要写成"Gemini 会冷读"的样子——独立成篇。

---

## 2. 协作循环（The Loop）

每一个工作单元——一个假设、一个 feature、一个决定——都跑同一个六阶段循环：

```
  FRAME ──▶ BUILD ──▶ VERIFY ──▶ DECIDE ──▶ PERSIST ──▶ HANDOFF
 (Elenka)  (Claude)  (Claude+   (Elenka)   (Claude)    (Claude)
                      CI/Gemini)                          │
     ▲───────────────────────────────────────────────────┘
        下个 session 从 FRAME 恢复，零重新推导
```

1. **Frame** — Elenka 给出唯一的问题。Claude 复述问题 + 相关约束（成本 gate、
   实盘红线），把 scope 说清楚。
2. **Build** — Claude 在 feature 分支上执行（git SOP R1–R4；见
   [feedback-git-workflow]）。
3. **Verify** — CI 绿 + deflation 统计（WFA∧CPCV，PSR/DSR/MinTRL/PBO）。任何
   非显然的结果、或同一问题连续 3 次卡住，走 `/architect-review` 给 Gemini。
4. **Decide** — 只有 Elenka 能跨越不可逆红线（合并 live 代码、晋级到 CTP、
   数据花费）。Claude 绝不 auto-merge 会影响实盘的代码。
5. **Persist** — 结论落到 **in-repo 真源**（R6），不是只进 memory。
6. **Handoff** — `/wrap-up-session` 把 memory 对账 git，打印 resume hook。

---

## 3. 知识契约：每类事实只有一个真源——memory 指路，git 回答

漂移发生在同一个事实存在于两个地方、其中一个变旧的时候。所以**每类事实只有
一个家，其余全部只做指针**。凡是 `git`/`gh` 能回答的，memory 不得复述。

| 事实类型 | Single source of truth | Memory 的职责 |
|------|------|------|
| 代码 / 测试 / 配置 | 仓库 | 无 |
| PR / 分支 / 合并状态 | **git**（`gh pr list --state all`） | 绝不断言——只指路 |
| 研究结论 + 证据 | `docs/research-findings.md` + 白名单 CSV | 一行指针 |
| 不可推导的 *why*（决定、偏好、SOP） | **auto-memory** | 这才是它真正的活 |
| "现在到哪了" | 每个活跃 workstream 一个 **current-state 指针文件** | 每 session 覆盖 |

**承重规则：memory 指路，git 回答。** 凡 `gh`/`git log` 能告诉你的，memory 不得
复述——那正是最容易变旧的一类事实。

---

## 4. 冷启动阅读顺序（最少 token，无损质量）

一个未来 session 按固定的分层顺序定位，能行动就立刻停：

- **Tier 0 — 免费且永远实时**（自动加载 + 3 条廉价命令）：`MEMORY.md` 索引，
  然后 `git status` · `git log --oneline -5` · `gh pr list --state all`。这是
  git-truth，**永不过期**。
- **Tier 1 — 读一个文件**：活跃 workstream 的 current-state 指针
  （如 `project-research-current-verdict` 或 `project-next-session-queue`）。
- **Tier 2 — 按需**：那个指针指向的 in-repo doc（`docs/research-findings.md`）。
  只在真正对它动手时读。
- **Tier 3 — 罕见**：深档（`project-research-layer2-status`）/ 单个
  `research/hX_*.py`。只为某个具体细节。

多数恢复止步于 Tier 1。这就是"最少 token 无损质量"的关键：Tier 0 是实时 git，
所以砍读取从不牺牲正确性。

---

## 5. 保鲜：写之前先对账（Reconcile before you write）

每次 `/wrap-up-session`（或任何 memory 更新）：**先把 memory 的断言 diff 对
git 现实**——已合并的 PR、已删的分支、已交付的结论——**改正漂移后再存**。
current-state 文件是覆盖（快照）；deep-archive 文件是追加。**一个 current-state
文件把某个 PR 号写成"open"就是坏味道**：换成实时 `gh` 检查，或删掉这条断言。

### 触发本规则的事故（2026-07-15）

`project-next-session-queue`（11 天旧）仍把 PR #18/#19/#21 列为 open 的
"awaiting Elenka" 决定——三个全都 MERGED，且本地 `main` 也落后于 origin。冷读
这条 memory 会浪费 token 重查已定案的工作，甚至基于错误前提行动。修复是一份
契约（§3–§4），不是"更自律"。这是 [feedback-git-workflow] R6（持久化研究知识）
的 resume 侧姊妹规则。

---

## 相关

- auto-memory `feedback-trinity-collaboration` — 三方角色与 3-retry 熔断
- auto-memory `feedback-git-workflow` — 分支/PR SOP 与 R1–R6
- auto-memory `feedback-handoff-protocol` — 本章程 §3–§5 的 Claude 执行精简版
- [research-findings.md](research-findings.md) — 研究结论真源（R6 的样板）
