# Signal Service — data → signal → Feishu

A standalone pipeline that turns bar data into trading signals and pushes a digest
to Feishu, **without** booting the vn.py GUI or the CTP gateway. This is the safe
first rung of "going live": a human receives the signal and decides. No orders are
placed.

```
data/bar/*.csv  ──▶  utils/signal_core.py  ──▶  signal_service.py  ──▶  飞书
   (OHLCV)          (pure strategy replay)        (digest + push)
```

> **⛔ 当前状态（2026-07-04）：所有出厂 job 已停用。**
> `config/signal_service.yaml` 原有的四个组合（RB/布林反转、JM/双均线、AG/唐奇安、
> I/双均线）在 [research-findings.md](research-findings.md) 的 master 结论里全部被
> 证伪，推送它们等于推送噪声。服务在无 enabled job 时 exit 2 —— 刻意让调度任务响亮
> 失败，而不是静默推送已证伪的信号。恢复推送的前提：合并 PR #18
> （`vol_target_ma_strategy`，唯一通过 DSR/PBO/成本/资金全部关卡的策略）、给
> `utils/signal_core.py` 增加对应复刻、并完成 live 候选的人工 go/no-go。

It shares the **exact** entry/exit logic of `strategies/*.py` via `utils/signal_core.py`
(asserted by `tests/test_signal_core.py`), reuses the hardened Feishu sender in
`utils/notifier.py` (`post_feishu`: HMAC signing, retry/backoff, v1/v2 schema), and
records every fresh signal to `logs/signals.jsonl` through the same `FileSignalLog`
the live system writes to.

## Quickstart

```powershell
# print the digest, send nothing (use this first, always)
python signal_service.py --dry-run

# run all jobs and push the digest to Feishu
python signal_service.py

# push only when a fresh entry/exit signal actually fired (quiet on no-op days)
python signal_service.py --only-on-signal
```

Exit code is non-zero if any job errors **or** the Feishu push fails — so a
scheduler surfaces both data problems and delivery problems instead of silently
failing. (This is deliberate: `notifier.send()` is fire-and-forget; the service
uses the synchronous `post_feishu` so a scheduled run *knows* the alert landed.)

## Configuration — `config/signal_service.yaml`

Each job names a bar CSV (resolved under `data/bar/`, override with `--data-dir`),
a strategy, and its parameters:

```yaml
defaults:
  warn_if_stale_days: 5        # flag a job whose newest bar is older than this

jobs:
  - name: 螺纹钢 RB · 布林反转
    underlying: RB
    data_file: rb_continuous_adj15_daily.csv
    strategy: boll_reversal     # double_ma | donchian | boll_reversal
    params: { boll_window: 20, boll_dev: 2.0 }
    enabled: true
```

`params` map 1:1 to the corresponding strategy's `parameters`. Plug in your
WFA-validated values per contract.

## What the digest contains

* **新信号** — entry/exit actions that fired on the most recent **closed** bar
  (开多 / 平多 / 开空 / 平空). This is the actionable part.
* **当前持仓建议** — the stance each strategy would currently hold (持多 / 持空 / 空仓),
  plus the last close and bar date.
* **数据陈旧** warnings — any series whose newest bar is older than `warn_if_stale_days`.
  Do not act on stale signals; refresh data first.

## Scheduling

Run it on a cadence aligned to your bar interval (e.g. once after the daily close):

```powershell
# Windows Task Scheduler example — daily 15:10
schtasks /Create /SC DAILY /ST 15:10 /TN QuantSignal ^
  /TR "python C:\Quant\signal_service.py --only-on-signal"
```

Refresh the data first in the same job (see `import_data.py` / `utils/data_fetcher.py`),
otherwise the stale-data guard will (correctly) flag everything.

## Go-live ladder (read before trusting this with money)

1. **`--dry-run`** until the digest looks right on your data.
2. **Human-in-the-loop alerts** (this service, as-is): you place orders manually
   from the Feishu message. Safe, reversible, and the recommended steady state.
3. **`QUANT_MODE=SIGNAL_ONLY`** in `run.py`: the live event loop runs end-to-end
   with synthetic fills — validates the *order path* (RiskGuard, SignalLog) without
   sending real orders.
4. **SimNow paper account** via CTP (`docs/simnow_preflight.md`).
5. **Auto-execution with real money** — only after the above, with `max_position_*`
   and `max_daily_loss_pct` limits set conservatively in `run.py`.

## Important caveats

* The `*_continuous_adj15_daily.csv` files are **research proxies** (back-adjusted
  continuous series). For live decisions, point `data_file` at the **actual main
  contract** you trade, refreshed before each run.
* Per the Layer-② master findings (`docs/research-findings.md`, which supersede the
  2026-05 phase report), **every strategy×instrument combo this service can currently
  run is falsified** — including RB/BollReversal, whose earlier "stable pattern" claim
  rested on an IS-OOS correlation of −0.60 (the signature of noise extraction, per
  methodology lesson #1). The only validated strategy is vol-target AG-solo (PR #18),
  which `signal_core` does not support yet. Until that lands, this service has nothing
  honest to push — which is why all jobs ship `enabled: false`.
