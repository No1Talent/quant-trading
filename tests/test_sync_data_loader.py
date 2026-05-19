"""sync_data_loader 纯函数测试：聚合逻辑 + 文件加载。

测试覆盖：
    - aggregate_positions：单策略、多策略同标的、长短抵消、孤儿数据、缺 vt_symbol、pos 非整数
    - load_local_positions_for_reconcile：文件缺失、空文件、损坏文件、正常路径
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.sync_data_loader import (
    aggregate_positions,
    load_local_positions_for_reconcile,
)


class TestAggregatePositions:
    def test_empty_inputs(self):
        assert aggregate_positions({}, {}) == {}

    def test_single_long_strategy(self):
        setting = {"s1": {"vt_symbol": "ag2506.SHFE", "class_name": "X", "setting": {}}}
        data = {"s1": {"pos": 3}}
        assert aggregate_positions(setting, data) == {"ag2506.SHFE": ("LONG", 3)}

    def test_single_short_strategy(self):
        setting = {"s1": {"vt_symbol": "ag2506.SHFE", "class_name": "X", "setting": {}}}
        data = {"s1": {"pos": -2}}
        assert aggregate_positions(setting, data) == {"ag2506.SHFE": ("SHORT", 2)}

    def test_multi_strategy_same_symbol_sum(self):
        setting = {
            "s1": {"vt_symbol": "i2509.DCE", "class_name": "X", "setting": {}},
            "s2": {"vt_symbol": "i2509.DCE", "class_name": "Y", "setting": {}},
        }
        data = {"s1": {"pos": 3}, "s2": {"pos": 2}}
        assert aggregate_positions(setting, data) == {"i2509.DCE": ("LONG", 5)}

    def test_multi_strategy_long_short_cancels(self):
        setting = {
            "s1": {"vt_symbol": "i2509.DCE", "class_name": "X", "setting": {}},
            "s2": {"vt_symbol": "i2509.DCE", "class_name": "Y", "setting": {}},
        }
        data = {"s1": {"pos": 3}, "s2": {"pos": -3}}
        # 净 0 → 不出现
        assert aggregate_positions(setting, data) == {}

    def test_multi_strategy_partial_net(self):
        setting = {
            "s1": {"vt_symbol": "i2509.DCE", "class_name": "X", "setting": {}},
            "s2": {"vt_symbol": "i2509.DCE", "class_name": "Y", "setting": {}},
        }
        data = {"s1": {"pos": 5}, "s2": {"pos": -3}}
        assert aggregate_positions(setting, data) == {"i2509.DCE": ("LONG", 2)}

    def test_zero_pos_skipped(self):
        setting = {"s1": {"vt_symbol": "ag2506.SHFE", "class_name": "X", "setting": {}}}
        data = {"s1": {"pos": 0}}
        assert aggregate_positions(setting, data) == {}

    def test_setting_without_data_treated_as_zero(self):
        # 首次启动 / 策略已配置但还没 sync 过
        setting = {"s1": {"vt_symbol": "ag2506.SHFE", "class_name": "X", "setting": {}}}
        data: dict = {}
        assert aggregate_positions(setting, data) == {}

    def test_orphan_data_logged_and_skipped(self, caplog):
        # data 有 setting 没 → 孤儿，跳过
        setting: dict = {}
        data = {"orphan": {"pos": 5}}
        with caplog.at_level("WARNING"):
            result = aggregate_positions(setting, data)
        assert result == {}
        assert any("孤儿策略" in r.message for r in caplog.records)

    def test_missing_vt_symbol_skipped(self, caplog):
        setting = {"s1": {"class_name": "X", "setting": {}}}  # 缺 vt_symbol
        data = {"s1": {"pos": 5}}
        with caplog.at_level("WARNING"):
            result = aggregate_positions(setting, data)
        assert result == {}
        assert any("缺 vt_symbol" in r.message for r in caplog.records)

    def test_non_integer_pos_falls_back_to_zero(self, caplog):
        setting = {"s1": {"vt_symbol": "ag2506.SHFE", "class_name": "X", "setting": {}}}
        data = {"s1": {"pos": "not-an-int"}}
        with caplog.at_level("WARNING"):
            result = aggregate_positions(setting, data)
        assert result == {}
        assert any("不是整数" in r.message for r in caplog.records)

    def test_three_instruments_h4_ensemble_shape(self):
        # 与 H4 ensemble 一致的实战形态
        setting = {
            "ag_dm": {"vt_symbol": "ag2506.SHFE", "class_name": "DoubleMa", "setting": {}},
            "i_dm": {"vt_symbol": "i2509.DCE", "class_name": "DoubleMa", "setting": {}},
            "cu_dm": {"vt_symbol": "cu2506.SHFE", "class_name": "DoubleMa", "setting": {}},
        }
        data = {
            "ag_dm": {"pos": 3},
            "i_dm": {"pos": -12},
            "cu_dm": {"pos": 1},
        }
        result = aggregate_positions(setting, data)
        assert result == {
            "ag2506.SHFE": ("LONG", 3),
            "i2509.DCE": ("SHORT", 12),
            "cu2506.SHFE": ("LONG", 1),
        }


class TestLoadFromDisk:
    def test_missing_dir_returns_empty(self, tmp_path: Path):
        result = load_local_positions_for_reconcile(tmp_path / "does-not-exist")
        assert result == {}

    def test_missing_files_returns_empty(self, tmp_path: Path):
        # 目录存在但无 sync 文件 → 等价首次启动
        assert load_local_positions_for_reconcile(tmp_path) == {}

    def test_empty_files_returns_empty(self, tmp_path: Path):
        (tmp_path / "cta_strategy_setting.json").write_text("", encoding="utf-8")
        (tmp_path / "cta_strategy_data.json").write_text("  ", encoding="utf-8")
        assert load_local_positions_for_reconcile(tmp_path) == {}

    def test_normal_path_roundtrip(self, tmp_path: Path):
        setting = {"s1": {"vt_symbol": "ag2506.SHFE", "class_name": "DoubleMa", "setting": {}}}
        data = {"s1": {"pos": 2, "fast_ma0": 19000.0}}
        (tmp_path / "cta_strategy_setting.json").write_text(json.dumps(setting), encoding="utf-8")
        (tmp_path / "cta_strategy_data.json").write_text(json.dumps(data), encoding="utf-8")
        result = load_local_positions_for_reconcile(tmp_path)
        assert result == {"ag2506.SHFE": ("LONG", 2)}

    def test_corrupt_setting_raises(self, tmp_path: Path):
        (tmp_path / "cta_strategy_setting.json").write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ValueError, match="sync_data 文件损坏"):
            load_local_positions_for_reconcile(tmp_path)

    def test_root_not_dict_raises(self, tmp_path: Path):
        (tmp_path / "cta_strategy_setting.json").write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="不是 dict"):
            load_local_positions_for_reconcile(tmp_path)

    def test_only_setting_no_data_returns_empty(self, tmp_path: Path):
        # 真实场景：策略已加载入 GUI 但还没产生过交易 / sync_data
        setting = {"s1": {"vt_symbol": "ag2506.SHFE", "class_name": "DoubleMa", "setting": {}}}
        (tmp_path / "cta_strategy_setting.json").write_text(json.dumps(setting), encoding="utf-8")
        result = load_local_positions_for_reconcile(tmp_path)
        assert result == {}
