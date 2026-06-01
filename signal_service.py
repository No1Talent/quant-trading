"""signal_service.py — standalone data → signal → Feishu pipeline.

Why this exists
---------------
Until now, signal generation lived *only* inside the live vn.py GUI (run.py),
bolted to the CTP gateway. There was no way to go "bars in → signal out → Feishu"
without booting the whole Windows/CTP trading stack. This script is that path:

    bar CSV  ──▶  utils.signal_core (pure replay)  ──▶  Feishu digest

It shares the *exact* entry/exit logic of strategies/*.py (via signal_core), reuses
the hardened Feishu sender in utils/notifier.py (HMAC signing, retry, dedup), and
records every fresh signal to logs/signals.jsonl through the existing FileSignalLog.

It does **not** place orders. It tells a human "RB just printed a 平多 signal" and
lets them act — the safest first rung of going live (see docs/signal-service.md).

Usage
-----
    python signal_service.py                 # run all jobs, push digest to Feishu
    python signal_service.py --dry-run        # build + print the digest, send nothing
    python signal_service.py --only-on-signal # push only if a fresh signal fired
    python signal_service.py --config config/signal_service.yaml --data-dir data/bar

Exit code is non-zero if any job errors (so a scheduler/cron surfaces failures).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml  # type: ignore[import-untyped]

from utils.notifier import build_session, load_feishu_config, post_feishu
from utils.signal_core import Action, replay_dataframe
from utils.signal_log import FileSignalLog, set_signal_log

REPO_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("signal_service")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_bars(path: Path) -> pd.DataFrame:
    """Load an OHLCV CSV (datetime,open,high,low,close,volume,open_interest)."""
    df = pd.read_csv(path)
    if "datetime" not in df.columns:
        raise ValueError(f"{path.name}: missing 'datetime' column")
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if df.empty:
        raise ValueError(f"{path.name}: no usable rows after cleaning")
    return df


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------
@dataclass
class JobResult:
    name: str
    underlying: str
    strategy: str
    last_dt: datetime | None = None
    last_close: float | None = None
    stance_label: str = "?"
    fresh: list[Action] = field(default_factory=list)
    stale: bool = False
    error: str | None = None


def run_job(job: dict, data_dir: Path, warn_stale_days: int) -> JobResult:
    name = job.get("name", job.get("underlying", "?"))
    res = JobResult(
        name=name,
        underlying=job.get("underlying", "?"),
        strategy=job.get("strategy", "?"),
    )
    try:
        df = load_bars(data_dir / job["data_file"])
        replay = replay_dataframe(job["strategy"], df, job.get("params", {}))
        last = df.iloc[-1]
        res.last_dt = last["datetime"].to_pydatetime()
        res.last_close = float(last["close"])
        res.stance_label = replay.stance_label_cn
        res.fresh = replay.last_bar_actions
        assert res.last_dt is not None  # set from df above; narrows for mypy
        res.stale = res.last_dt < datetime.now() - timedelta(days=warn_stale_days)
    except Exception as e:  # noqa: BLE001 — one bad job must not sink the run
        res.error = str(e)
        logger.exception("job %s failed", name)
    return res


# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------
def build_digest(results: list[JobResult], data_dir: Path) -> tuple[str, int]:
    """Return (feishu_message, fresh_signal_count)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    fresh = [(r, a) for r in results for a in r.fresh]
    lines: list[str] = [
        "📡 量化信号播报",
        f"时间 {now} ｜ 数据 {data_dir.as_posix()} ｜ 任务 {len(results)}",
    ]

    lines.append("")
    if fresh:
        lines.append(f"🔔 新信号 ({len(fresh)})")
        for r, a in fresh:
            dt = a.dt.strftime("%Y-%m-%d") if a.dt else "?"
            lines.append(f"• {r.name} ｜ {a.label_cn} @ {a.price:g}  (bar {dt})")
    else:
        lines.append("🔕 本次无新开/平仓信号")

    lines.append("")
    lines.append("📊 当前持仓建议")
    for r in results:
        if r.error:
            lines.append(f"• {r.name}：⚠️ 出错 {r.error}")
            continue
        dt = r.last_dt.strftime("%Y-%m-%d") if r.last_dt else "?"
        tag = " ⏳数据陈旧" if r.stale else ""
        lines.append(f"• {r.name}：{r.stance_label} ｜ 收{r.last_close:g} ({dt}){tag}")

    stale = [r for r in results if r.stale and not r.error]
    if stale:
        lines.append("")
        lines.append(
            "⚠️ 数据陈旧（请刷新行情后再据此操作）: " + "、".join(r.underlying for r in stale)
        )

    errors = [r for r in results if r.error]
    if errors:
        lines.append("")
        lines.append("❌ 失败任务: " + "、".join(r.name for r in errors))

    return "\n".join(lines), len(fresh)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data → signal → Feishu")
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "signal_service.yaml"))
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data" / "bar"))
    parser.add_argument("--dry-run", action="store_true", help="print digest, send nothing")
    parser.add_argument(
        "--only-on-signal", action="store_true", help="push only if a fresh signal fired"
    )
    parser.add_argument("--no-log", action="store_true", help="do not append to signals.jsonl")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [signal_service] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    data_dir = Path(args.data_dir)
    warn_stale_days = config.get("defaults", {}).get("warn_if_stale_days", 5)
    jobs = [j for j in config.get("jobs", []) if j.get("enabled", True)]
    if not jobs:
        logger.error("no enabled jobs in %s", args.config)
        return 2

    if not args.no_log and not args.dry_run:
        set_signal_log(FileSignalLog(REPO_ROOT / "logs" / "signals.jsonl"))

    results = [run_job(j, data_dir, warn_stale_days) for j in jobs]

    # Record fresh signals to the same JSONL stream the live system writes to.
    if not args.no_log and not args.dry_run:
        from utils.signal_log import get_signal_log

        slog = get_signal_log()
        for r in results:
            for a in r.fresh:
                slog.append(
                    strategy_name=f"signal_service:{r.strategy}",
                    vt_symbol=r.underlying,
                    side=a.side,
                    price=a.price,
                    volume=1,
                    allowed=True,
                    reject_reason=None,
                    metadata={"source": "signal_service", "bar": str(a.dt)},
                )

    message, n_fresh = build_digest(results, data_dir)
    print("\n" + message + "\n")

    if args.dry_run:
        logger.info("dry-run: not sending")
    elif args.only_on_signal and n_fresh == 0:
        logger.info("no fresh signal and --only-on-signal set: not sending")
    else:
        feishu = load_feishu_config()
        if not (feishu and feishu.get("enabled") and feishu.get("webhook")):
            logger.error(
                "飞书未启用或缺少 webhook（检查 vnpy_workspace/notify_config.json）— 未发送"
            )
            return 1
        # Synchronous + verifiable: a scheduled run must know whether the alert
        # actually landed, so we exit non-zero on delivery failure rather than
        # fire-and-forget. Reuses the notifier's exact Feishu wire format.
        session = build_session()
        try:
            post_feishu(
                session,
                feishu["webhook"],
                message,
                secret=feishu.get("secret"),
                at_all=bool(feishu.get("at_all")),
            )
            logger.info("✅ 已推送到飞书（%d 条新信号）", n_fresh)
        except Exception as e:  # noqa: BLE001
            logger.error("❌ 飞书推送失败: %s", e)
            return 1
        finally:
            session.close()

    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
