"""读取 vn.py CtaStrategyApp 的 sync_data，转成对账器所需的 vt_symbol → 仓位映射。

工作流：
    1. 读 .vntrader/cta_strategy_setting.json     ← strategy_name → vt_symbol
    2. 读 .vntrader/cta_strategy_data.json        ← strategy_name → {pos, ...}
    3. 按 vt_symbol 聚合 signed pos（多策略同标的 → 求和）
    4. sign(sum) → direction，abs(sum) → volume；净 0 跳过

约定：
    - vn.py CtaTemplate.pos 是 signed int：正=多头净仓，负=空头净仓
    - 同一 vt_symbol 多策略共存时，最终仓位 = 各策略 pos 代数和
    - 若策略 in setting 而 not in data（vn.py 第一次跑前尚无 sync_data），按 pos=0 处理
    - 若策略 in data 而 not in setting（残留孤儿数据），记录 WARN 并跳过：没有 vt_symbol
      无法去 CTP 对照

设计为纯函数 + 路径注入，便于测试。run.py 调用 load_local_positions_for_reconcile()
即可拿到 reconciler.run_reconcile() 直接消费的字典。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("sync_data_loader")

DEFAULT_SETTING_FILENAME = "cta_strategy_setting.json"
DEFAULT_DATA_FILENAME = "cta_strategy_data.json"


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    """读 JSON；不存在返回 {}，损坏抛 ValueError（fail-fast 由调用方决定）。"""
    if not path.exists():
        logger.info("sync_data 文件不存在（首次启动可正常）：%s", path)
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            logger.info("sync_data 文件为空：%s", path)
            return {}
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"sync_data 文件损坏 {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"sync_data 文件根节点不是 dict：{path}")
    return data


def aggregate_positions(
    setting: dict[str, dict[str, Any]],
    data: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, int]]:
    """纯函数：按 vt_symbol 聚合 signed pos，返回 reconciler 所需格式。

    输入：
        setting — {strategy_name: {"class_name", "vt_symbol", "setting"}}
        data    — {strategy_name: {"pos", ...}}

    输出：
        {vt_symbol: ("LONG"|"SHORT", abs_volume)}，净 0 的标的不出现。
    """
    # 警告：data 中有但 setting 没有的策略（孤儿）—— 无 vt_symbol 无法对账
    orphan_data = set(data) - set(setting)
    for name in sorted(orphan_data):
        logger.warning(
            "sync_data 中存在孤儿策略 '%s'（无对应 setting，无法解析 vt_symbol），跳过",
            name,
        )

    # 按 vt_symbol 累加 signed pos
    signed_by_symbol: dict[str, int] = {}
    for strategy_name, strategy_cfg in setting.items():
        vt_symbol = strategy_cfg.get("vt_symbol")
        if not vt_symbol:
            logger.warning("策略 '%s' 缺 vt_symbol 字段，跳过", strategy_name)
            continue
        # 缺 data 项 = 还没产生过 sync 数据 = pos 0
        pos_raw = data.get(strategy_name, {}).get("pos", 0)
        try:
            pos = int(pos_raw)
        except (TypeError, ValueError):
            logger.warning("策略 '%s' 的 pos 不是整数（=%r），按 0 处理", strategy_name, pos_raw)
            pos = 0
        if pos == 0:
            continue
        signed_by_symbol[vt_symbol] = signed_by_symbol.get(vt_symbol, 0) + pos

    # 转 (direction, abs_volume) 形式；净 0 跳过
    out: dict[str, tuple[str, int]] = {}
    for vt_symbol, signed in signed_by_symbol.items():
        if signed == 0:
            continue
        direction = "LONG" if signed > 0 else "SHORT"
        out[vt_symbol] = (direction, abs(signed))
    return out


def load_local_positions_for_reconcile(
    vntrader_dir: Path | str,
    *,
    setting_filename: str = DEFAULT_SETTING_FILENAME,
    data_filename: str = DEFAULT_DATA_FILENAME,
) -> dict[str, tuple[str, int]]:
    """加载并聚合，得到 reconciler.run_reconcile() 直接可用的字典。

    抛 ValueError 当任一文件损坏。文件不存在按"无策略"处理 → 返回 {}。
    """
    base = Path(vntrader_dir)
    setting = _read_json_or_empty(base / setting_filename)
    data = _read_json_or_empty(base / data_filename)

    positions = aggregate_positions(setting, data)
    logger.info(
        "本地 sync_data 解析完成：%d 个策略 setting，%d 个有 data，聚合后 %d 个非零仓位",
        len(setting),
        len(data),
        len(positions),
    )
    for vt_symbol, (direction, volume) in sorted(positions.items()):
        logger.info("  本地仓位：%s %s %d", vt_symbol, direction, volume)
    return positions
