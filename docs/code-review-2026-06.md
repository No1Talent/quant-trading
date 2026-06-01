# Code Review — Quant (vn.py + CTP platform)

**Date:** 2026-06-01 · **Reviewer:** Claude · **Scope:** whole repo, with focus on the
data → signal → Feishu path the owner asked to make production-usable.

## Verdict

This is a **strong, mature codebase** — well above the median for a personal/desk
quant stack. The infrastructure (notifications, risk gating, reconciliation, run
modes, research harness) is thoughtfully designed, documented, and tested. The
honest gap is not engineering quality; it is that (a) the **alpha is unproven** by
the project's own research, and (b) the **fully-automated live path can't run or be
tested off Windows+CTP**, so "real trade now" should mean *signal alerting to a
human*, which this session makes real and verifiable.

## What it is

A vn.py/CTP futures workspace that adds, on top of stock vn.py: multi-channel
alerting (email / WeCom / Server酱 / DingTalk / Feishu) that is fully **decoupled
from strategy code** via the event bus; an engine-level **risk pre-gate**
(`RiskGuard`) with daily-loss / per-symbol / per-underlying / trade-rate breakers;
a **startup reconciler** that refuses to launch on a CTP-vs-local position
mismatch; three run modes (`LIVE` / `SIGNAL_ONLY` / `REPLAY`) with sandboxed
working dirs so synthetic fills never pollute live `self.pos`; a structured
**signal log** (JSONL); a **product/rollover registry**; and a serious research
layer (per-contract walk-forward + combinatorial purged CV).

## What's genuinely good

The strategy ⊥ notification decoupling is the standout decision. Strategies only
call `write_log()`; a `NotifyListener` on the event bus translates events into
pushes. That kills the classic `super().on_start()`-forgotten footgun, makes
backtests side-effect-free, and lets the notifier be unit-tested in isolation.
The `RiskGuard` deliberately **does not auto-liquidate** — it halts, alerts, and
drops a breach flag for a human; correct for a system that can be wrong. The
`SIGNAL_ONLY`/`REPLAY` cwd isolation (so `cta_strategy_data.json` can't leak fake
positions into LIVE) shows real operational scar tissue. Testing is broad (~28
files), CI runs ruff + mypy + pytest, and the docs (`architecture.md`,
`operations.md`, `roadmap.md`) are unusually honest — including writing down that
most strategies showed **no durable edge**.

## Is it ready for real trade?

Split the question by what "trade" means:

* **Signal alerting to a human (recommended now):** Yes — and this session makes
  it a real, tested pipeline (`signal_service.py`). Generate signals from data,
  push a digest to Feishu, a person decides. Low blast radius, fully reversible.
* **Automated execution with real money:** Not yet, and not from this machine.
  Three blockers: (1) the live `.vntrader/database.db` is **empty** (36 KB header
  only) — `load_bar(N)` returns 0 rows and strategy init fails until you import
  data; (2) the CTP gateway needs Windows + compiled DLLs + a funded/SimNow
  account, so the live path is untestable in CI and was not exercised here; (3)
  most important, the **edge is unproven** — by your own WFA, only RB/BollReversal
  was a stable out-of-sample pattern, and even "62% positive folds" coexisted with
  negative IS-OOS correlation. Auto-trading an unproven signal is the expensive
  way to find that out.

The right path to live is the ladder in `docs/signal-service.md`:
dry-run → human-in-loop alerts → `SIGNAL_ONLY` → SimNow → (only then) auto-exec
with conservative `max_position_*` / `max_daily_loss_pct`.

## Issues & risks found (prioritized)

1. **Unproven alpha (highest).** Infra quality is masking that the signals may not
   make money. Before risking capital, decide the bar: e.g. only deploy a
   strategy×instrument whose WFA shows positive median OOS Sharpe *and* non-negative
   IS-OOS correlation, sized small.
2. **Live data not loaded.** The live DB is empty; nothing trades until
   `import_data.py` / `utils/data_fetcher.py` populate it. Make data-freshness a
   pre-trade gate (the new signal service already warns on stale bars).
3. **Delivery was unverifiable.** `notifier.send()` is fire-and-forget, so a failed
   push was invisible to the caller — a scheduled signal job could "succeed" while
   delivering nothing. *Fixed this session* for the service via a synchronous,
   status-returning `post_feishu`; the fan-out `NotifyListener` path still can't
   confirm delivery (acceptable for redundant alerts, worth knowing).
4. **Plaintext credentials on disk.** `vnpy_workspace/notify_config.json` holds a
   live Feishu webhook + signing secret (it is correctly git-ignored, so not in
   history — good). CTP password is plaintext too. Roadmap P0-1 (system keyring)
   is the right fix; until then, treat that file as a secret and rotate the Feishu
   secret if this repo was ever shared.
5. **Single-platform live path.** CTP binds the whole live stack to Windows; CI
   can't cover the gateway. `REPLAY` mode mitigates by exercising the event/strategy
   path headlessly — keep investing there.
6. **No strategy PnL regression test (roadmap P0-4).** A one-character change to
   signal logic can pass everything and lose money live. Partially addressed now:
   `utils/signal_core.py` pins the entry/exit logic in pure tests; extend toward
   asserting backtest PnL/'#trades' on fixed fixtures.

Minor: dedup keys on Python's randomized `hash(str)` (fine within a process, not
across restarts); substring keyword matching in `NotifyListener` is pragmatic but
will occasionally mis-route — both are already acknowledged in comments.

## What this session added

* `utils/signal_core.py` — pure-Python (numpy) replays of double-MA / Donchian /
  Bollinger that **mirror `strategies/*.py` exactly**, runnable with no vnpy/CTP.
* `tests/test_signal_core.py` — parity/behaviour tests (pass on Linux CI).
* `signal_service.py` + `config/signal_service.yaml` — the standalone
  data → signal → Feishu service (dry-run / only-on-signal, stale-data guard,
  writes to `logs/signals.jsonl`, non-zero exit on delivery failure).
* `utils/notifier.py` — extracted reusable `build_session()` + `post_feishu()` +
  `load_feishu_config()`; `_send_feishu` now delegates (no behaviour change; all 27
  notifier tests still pass). This is what gives the service a *verifiable* send.
* `docs/signal-service.md` — run guide, scheduling, and the go-live ladder.

Verification: 73 vnpy-free tests pass (notifier + signal_core + product_registry +
rollover + factors); the service produces a correct digest from the real CSVs and,
against a local mock Feishu endpoint, completed the full signed push (exit 0,
payload carried `timestamp`+`sign`). The only thing not exercised is the outbound
call to `open.feishu.cn`, which this sandbox blocks at the proxy — it will deliver
from your machine, where Feishu is reachable.

## Recommended next steps

1. Run `python signal_service.py --dry-run`, then wire it to your real main-contract
   data and schedule it (`--only-on-signal`) — start getting signals to Feishu daily.
2. Set an explicit, written deploy bar for alpha; only promote strategies that clear it.
3. Close roadmap P0-1 (keyring) and P0-4 (strategy PnL regression) before any
   auto-execution.
4. Keep humans in the loop until SimNow results match backtest order-by-order
   (roadmap P2-11 contract test).
