"""Drift guard: every strategy in strategies/ must use safe_* wrappers, not raw self.buy/sell/short/cover.

Why this test exists
--------------------
The safe_* wrappers in utils.strategy_base wire two production safety paths into
every order:

  1. RiskGuard.check_order_pre()  — price-deviation / stale-tick / tripped-state gate
  2. SignalLog.append(...)        — JSONL row used for LIVE vs SIGNAL_ONLY diff

When strategies were first written, only DoubleMa was migrated; the rest silently
bypassed both paths. This test scans the AST of each strategy class and fails on
``self.buy(...) / self.sell(...) / self.short(...) / self.cover(...)``. New
strategies copied from old templates that forget the wrapper get caught here
instead of at 09:01:30 in 实盘.

The scan is intentionally pure-AST (no import) so it runs on CI without vnpy
and doesn't slow down the rest of the suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"
FORBIDDEN_METHODS = {"buy", "sell", "short", "cover"}


def _find_raw_order_calls(source: str, file_label: str) -> list[str]:
    """Return human-readable locations of forbidden ``self.<method>(...)`` calls.

    Only checks calls whose attribute *target* is the bare name ``self`` — so
    ``self.bg.update_tick(...)`` and other unrelated attribute chains are safe.
    """
    tree = ast.parse(source, filename=file_label)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in FORBIDDEN_METHODS:
            continue
        target = func.value
        if isinstance(target, ast.Name) and target.id == "self":
            violations.append(f"{file_label}:{node.lineno} — self.{func.attr}(...)")
    return violations


def _strategy_modules() -> list[Path]:
    return sorted(
        p
        for p in STRATEGIES_DIR.glob("*.py")
        if p.name not in ("__init__.py",) and not p.name.startswith("_")
    )


@pytest.mark.parametrize("path", _strategy_modules(), ids=lambda p: p.name)
def test_strategy_uses_safe_wrappers(path: Path) -> None:
    """No production strategy may call ``self.buy/sell/short/cover`` directly."""
    source = path.read_text(encoding="utf-8")
    violations = _find_raw_order_calls(source, str(path.relative_to(STRATEGIES_DIR.parent)))
    assert not violations, (
        "Strategy bypasses safe_* wrappers — RiskGuard pre-gate and SignalLog tap are "
        "silently skipped. Replace raw self.<method>(...) with safe_<method>(self, ...):\n"
        + "\n".join("  " + v for v in violations)
    )
