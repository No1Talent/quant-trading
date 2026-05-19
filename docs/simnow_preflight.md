# SimNow 启动前检查清单

**目的**:首次 / 每次 SimNow 启动前过一遍,避免带病上线。

**适用阶段**:Layer ③ paper trade(P0-4)、Layer ③ 小资金实盘(P1)。

**单次过清单时间**:~10 分钟。

---

## 一、账号 & 凭证

- [ ] **SimNow 账号有效**:登录 [SimNow 官网](https://www.simnow.com.cn/)确认账号未失效;首次注册需要等待开户邮件,通常 1 个工作日内。
- [ ] **[vnpy_workspace/connect_ctp.json](../vnpy_workspace/connect_ctp.json) 已填**:
    - 模板:[connect_ctp.json.template](../vnpy_workspace/connect_ctp.json.template)
    - 经纪商代码:`9999`(SimNow 固定)
    - 交易服务器:`tcp://180.168.146.187:10130`(7×24)或 `tcp://180.168.146.187:10201`(白天)
    - 行情服务器:`tcp://180.168.146.187:10131`(7×24)或 `tcp://180.168.146.187:10211`(白天)
    - 产品名称:`simnow_client_test`(SimNow 公开值)
    - 授权编码:`0000000000000000`(SimNow 公开值)
- [ ] **`connect_ctp.json` 未提交到 git**:`git status` 应当看不到它(.gitignore 已配置)。
- [ ] **密码强度**:即便是 SimNow,也避免与真实账号同密码(防泄密扩散)。

## 二、通知通道

- [ ] **[vnpy_workspace/notify_config.json](../vnpy_workspace/notify_config.json) 已填**:模板见 [notify_config.json.template](../vnpy_workspace/notify_config.json.template)。
- [ ] **至少一个 CRITICAL 通道 enabled**:风控熔断 / 对账失败时必须能找到你。推荐企业微信 webhook(免费、即时)。
- [ ] **测试发送**:运行
  ```powershell
  python -c "from utils import get_notifier, NotifyLevel; get_notifier().send('preflight test', title='SimNow preflight', level=NotifyLevel.CRITICAL, force=True)"
  ```
  确认手机收到。
- [ ] **rate_limit 合理**:`rate_limit_per_minute: 30` 对实盘够用,paper trade 可调高到 60 防遗漏。
- [ ] **dedup_window 不要太长**:60s 防同一条告警刷屏,但如果策略快速触发多个不同警告,可调到 30s。

## 三、风控配置

- [ ] **[vnpy_workspace/run.py](../vnpy_workspace/run.py) 中 `attach_risk_guard` 参数适配你的资金量**:
    - `max_daily_loss_pct=0.05`:5% 日亏停机。SimNow 期可放宽到 0.10 观察行为,实盘必须收紧。
    - `max_position_per_symbol=10`:单标的 10 手上限。按 P0-2 自然手数:10M 资金 I 自然手数 12,需要调到 15。AG/CU 1M+ 1-3 手,默认够用。
    - `max_trades_per_minute=20`:CTP 高频限流自保。DoubleMa 日线策略一天 0-2 笔,这个限制基本不会触发,但反过来确认有兜底。
- [ ] **logs/ 目录可写**:`mkdir -p logs && touch logs/test.flag && rm logs/test.flag` 应无报错。
- [ ] **风控 flag 不存在**:
  ```powershell
  if (Test-Path logs/risk_breach.flag) { Write-Warning "PREVIOUS BREACH — 处理后再启动" }
  ```
- [ ] **对账 flag 不存在**:
  ```powershell
  if (Test-Path logs/reconcile_breach.flag) { Write-Warning "PREVIOUS RECONCILE BREACH — 处理后再启动" }
  ```

## 四、对账(P0-3 关键)

- [ ] **[utils/reconciler.py](../utils/reconciler.py) 和 [utils/sync_data_loader.py](../utils/sync_data_loader.py) 都在**:`python -c "from utils import run_reconcile, load_local_positions_for_reconcile"` 应无 import 错误。
- [ ] **接线测试通过**:`pytest tests/test_startup_reconcile_wiring.py -v` 应当 6/6 PASS。
- [ ] **预期本地仓位**:启动前你应当知道本地 `cta_strategy_data.json` 显示什么仓位。可手动检查:
  ```powershell
  Get-Content vnpy_workspace/.vntrader/cta_strategy_data.json -ErrorAction SilentlyContinue
  ```
  若文件不存在 = 首次启动 = local empty = 期望 CTP 也空。
- [ ] **若上次实盘留有持仓**:登录 SimNow Web/Client 确认仓位,确保和本地 sync_data 一致,否则启动期对账会 fail-fast。

## 五、数据与策略

- [ ] **数据库已就位**:`Test-Path "$env:USERPROFILE\.vntrader\database.db"` 应为 True。空数据库 = 策略 on_init 无法 load_bar = 策略报"未初始化"。
- [ ] **要交易的合约在数据库里**:`dbbaroverview` 表里能看到对应 vt_symbol。SimNow paper 阶段建议从 ag 主连合约的当月/季月开始。
- [ ] **策略配置和资金匹配**(参考 P0-2 结论 [research/h4d_capital_sizing_summary.csv](../research/h4d_capital_sizing_summary.csv)):
    - **< 1.5M 资金**:DD/cap 风险过高,**不要**跑 H4 ensemble 或 AG 单脚。要测试至少用 SimNow 模拟资金。
    - **1.5M - 2M**:可跑 AG 单脚(`fixed_size=1`),DD/cap ~10%。
    - **2M - 5M**:可跑全 ensemble,`fixed_size`:AG=1, I=1-2, CU=1。
    - **5M+**:`fixed_size` 按 P0-2 自然手数表。
- [ ] **WFA-locked 参数有记录**:每个策略实例的 `(fast_window, slow_window, fixed_size)` 来自哪个 [research/wfa_results_*.csv](../research/) 的哪一折,有据可查(否则三个月后你不知道为什么实盘是这组数)。

## 六、网络与时间

- [ ] **能 ping 通 SimNow**:`Test-NetConnection 180.168.146.187 -Port 10130`(夜盘)或 `-Port 10201`(日盘)应为 TcpTestSucceeded=True。
- [ ] **系统时间和实际时间偏差 < 1s**:CTP 对时间敏感,Windows Time Service 应启用。
- [ ] **首次启动选夜盘**:21:00-02:30 的 SimNow 夜盘通道(`:10130`)流量大、撮合频繁,容易暴露问题。日盘的 `:10201` 流量小,bug 难复现。

## 七、启动 & 首日观察

- [ ] **启动命令**:
  ```powershell
  cd c:\Quant\vnpy_workspace
  python run.py
  ```
- [ ] **启动序列预期日志**(按顺序):
    1. `vn.py 交易系统启动`
    2. (若上次 reconcile breach)→ 进程退出,先处理 flag
    3. `自动连接 CTP: ...`
    4. `Init-Settle-Quiet 通过(...)`(reconciler 内部)
    5. `✅ 启动期对账通过`
    6. GUI 出现
- [ ] **5 分钟内 sanity**:GUI 出现后立即:
    - 查看持仓页是否和你期望一致
    - 资金账户余额不为 0
    - 没有红色 CRITICAL 弹窗
- [ ] **首日 dashboard 已开**:见 [streamlit_live.py](../streamlit_live.py),提供实时 PnL/持仓/对账状态视图。
  ```powershell
  streamlit run streamlit_live.py
  ```

## 八、停机

- [ ] **绝不**用 `taskkill /F` 或关窗口结束 vn.py 进程 — 用 GUI 菜单"退出"或 Ctrl+C(后者依赖 vn.py 的信号处理)。
- [ ] **检查 sync_data 已写**:停机后 `cta_strategy_data.json` 的 mtime 应当刚被更新。
- [ ] **日志已 flush**:`logs/trader.log` 末尾应当看到"已干净退出"。

---

## 反向测试(P0-3 验证,可选,在 SimNow 接好前推荐做一次)

为验证 reconciler 真的会 catch 仓位幻觉,在 SimNow 完全配好但**还未启动策略**的状态下:

1. 启动 vn.py 一次,让 CTP 连上、对账空仓通过、GUI 出现。
2. 关闭 vn.py(走干净退出流程)。
3. 手工编辑 `vnpy_workspace/.vntrader/cta_strategy_setting.json` 加一条策略,并在 `cta_strategy_data.json` 加上 `{"pos": 1}` 模拟"上次留了 1 手"。
4. 再次启动 vn.py。
5. **期望**:
    - 日志 CRITICAL:`持仓对账不一致 1 条:[{'vt_symbol': '...', 'local': ('LONG', 1), 'ctp': None}]`
    - `logs/reconcile_breach.flag` 写入
    - 进程 `sys.exit(1)`,GUI 不出现
    - 收到 CRITICAL 通知
6. 删除 flag + 恢复 setting/data 文件,确认下次启动正常。

如果上述任一步不符,**不要上 paper trade**,先排查 P0-3 接线问题。
