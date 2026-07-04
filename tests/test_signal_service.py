"""signal_service.py 服务级冒烟测试。

这个文件存在的直接原因：signal_service.py 曾在 main 上直接 ImportError——
它 import 的 ``build_session``/``post_feishu``/``load_feishu_config`` 只存在于
未合并的 feat/signal-service-cleanup 分支，而全仓 545 个测试没有一个 import 过
signal_service，CI 全绿也拦不住一个起不来的入口。服务级入口必须有
import 守护 + 主路径冒烟，本文件补上这两层。

全部测试 vnpy-free（signal_service 本身的设计承诺），因此在 Ubuntu CI 也运行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import signal_service


def test_imports_cleanly():
    """import 本身就是断言：入口脚本的依赖面必须真实存在（防半合并事故复发）。"""
    assert callable(signal_service.main)


def _write_bars_csv(path: Path, n: int = 40) -> None:
    """V 形走势的合成日线：先跌后涨，足够触发 double_ma 金叉。"""
    lines = ["datetime,open,high,low,close,volume,open_interest"]
    closes = [100.0 - i for i in range(n // 2)] + [81.0 + i * 2 for i in range(n - n // 2)]
    for i, close in enumerate(closes):
        day = f"2026-01-{(i % 28) + 1:02d}" if i < 28 else f"2026-02-{(i - 28) + 1:02d}"
        lines.append(f"{day} 15:00:00,{close},{close + 1},{close - 1},{close},1000,5000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_config(path: Path, *, data_file: str, enabled: bool) -> None:
    path.write_text(
        f"""
defaults:
  warn_if_stale_days: 99999

jobs:
  - name: 测试品种 · 双均线
    underlying: TT
    data_file: {data_file}
    strategy: double_ma
    params: {{ fast_window: 3, slow_window: 5 }}
    enabled: {str(enabled).lower()}
""",
        encoding="utf-8",
    )


def test_no_enabled_jobs_exits_2(tmp_path: Path):
    """全部 job 停用时必须响亮失败（exit 2），不得静默成功。

    这是 2026-07-04 起的出厂状态：config/signal_service.yaml 的四个组合全部被
    research-findings.md 证伪而停用，调度任务应当报错而不是推送噪声。
    """
    cfg = tmp_path / "cfg.yaml"
    _write_config(cfg, data_file="tt.csv", enabled=False)
    rc = signal_service.main(["--config", str(cfg), "--data-dir", str(tmp_path), "--dry-run"])
    assert rc == 2


def test_dry_run_digest_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """dry-run 主路径：读 CSV → 回放 → 打印 digest → exit 0，不发送、不落 signals.jsonl。"""
    _write_bars_csv(tmp_path / "tt.csv")
    cfg = tmp_path / "cfg.yaml"
    _write_config(cfg, data_file="tt.csv", enabled=True)

    rc = signal_service.main(["--config", str(cfg), "--data-dir", str(tmp_path), "--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "量化信号播报" in out
    assert "测试品种 · 双均线" in out
    assert "当前持仓建议" in out


def test_job_error_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """坏 job 不沉没整次运行（digest 照出），但退出码必须非 0 让调度器看见。"""
    cfg = tmp_path / "cfg.yaml"
    _write_config(cfg, data_file="does_not_exist.csv", enabled=True)

    rc = signal_service.main(["--config", str(cfg), "--data-dir", str(tmp_path), "--dry-run"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "失败任务" in out
