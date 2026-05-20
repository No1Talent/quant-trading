"""ProductRegistry: vt_symbol -> underlying 解析 + YAML 验证。"""

from __future__ import annotations

from pathlib import Path

import pytest

from utils.product_registry import (
    DEFAULT_PRODUCTS_YAML,
    Product,
    ProductRegistry,
    RolloverConfig,
    get_default_registry,
    set_default_registry,
)


@pytest.fixture(autouse=True)
def _reset_default_registry():
    set_default_registry(None)
    yield
    set_default_registry(None)


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "products.yaml"
    p.write_text(content, encoding="utf-8")
    return p


class TestLoadAndParse:
    def test_load_default_yaml_has_known_products(self) -> None:
        # 项目自带的 config/products.yaml 必须 parse 通过且包含核心 4 个
        reg = ProductRegistry.load(DEFAULT_PRODUCTS_YAML)
        codes = set(reg.all_codes())
        assert {"RB", "JM", "AG", "I"}.issubset(codes)

    def test_empty_file_loads_empty_registry(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "")
        reg = ProductRegistry.load(p)
        assert reg.all_codes() == []

    def test_missing_field_raises_with_location(self, tmp_path: Path) -> None:
        p = _write_yaml(
            tmp_path,
            """
products:
  RB:
    name: 螺纹钢
    exchange: SHFE
    # missing symbol_pattern
    size: 10
    pricetick: 1.0
""",
        )
        with pytest.raises(ValueError, match="RB.*missing"):
            ProductRegistry.load(p)

    def test_invalid_regex_raises(self, tmp_path: Path) -> None:
        p = _write_yaml(
            tmp_path,
            """
products:
  RB:
    name: 螺纹钢
    exchange: SHFE
    symbol_pattern: '['  # broken regex
    size: 10
    pricetick: 1.0
""",
        )
        with pytest.raises(ValueError, match="invalid symbol_pattern"):
            ProductRegistry.load(p)

    def test_top_level_must_be_mapping(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, "- not-a-mapping\n")
        with pytest.raises(ValueError, match="'products' key"):
            ProductRegistry.load(p)


class TestUnderlyingResolution:
    @pytest.fixture
    def reg(self) -> ProductRegistry:
        return ProductRegistry.load(DEFAULT_PRODUCTS_YAML)

    def test_resolves_rb_main_contract(self, reg: ProductRegistry) -> None:
        assert reg.underlying_of("rb2410.SHFE") == "RB"
        assert reg.underlying_of("rb2501.SHFE") == "RB"

    def test_resolves_other_underlyings(self, reg: ProductRegistry) -> None:
        assert reg.underlying_of("jm2501.DCE") == "JM"
        assert reg.underlying_of("ag2506.SHFE") == "AG"
        assert reg.underlying_of("i2501.DCE") == "I"

    def test_case_insensitive_exchange(self, reg: ProductRegistry) -> None:
        # 研究脚本可能 vt_symbol 拼小写交易所，要照样解析
        assert reg.underlying_of("rb2410.shfe") == "RB"

    def test_unregistered_symbol_raises(self, reg: ProductRegistry) -> None:
        with pytest.raises(KeyError, match="未在 ProductRegistry 中注册"):
            reg.underlying_of("zzz9999.SHFE")

    def test_missing_exchange_segment_raises(self, reg: ProductRegistry) -> None:
        with pytest.raises(KeyError, match="缺少 exchange 段"):
            reg.underlying_of("rb2410")

    def test_wrong_exchange_does_not_match(self, reg: ProductRegistry) -> None:
        # RB 在 SHFE，写成 DCE 必须失败 — 避免跨交易所串台
        with pytest.raises(KeyError):
            reg.underlying_of("rb2410.DCE")

    def test_or_none_variant_does_not_raise(self, reg: ProductRegistry) -> None:
        assert reg.underlying_of_or_none("zzz9999.SHFE") is None
        assert reg.underlying_of_or_none("rb2410.SHFE") == "RB"

    def test_continuous_symbol_falls_through(self, reg: ProductRegistry) -> None:
        # 研究层的 i_continuous.DCE 不是物理合约 — 必须显式不解析为 I
        # （否则 RiskGuard 会把研究序列的虚拟仓位与实盘合约 i2501 汇总）
        assert reg.underlying_of_or_none("i_continuous.DCE") is None


class TestRolloverOverride:
    def test_default_rollover_inherited(self, tmp_path: Path) -> None:
        # 未声明 rollover → product.rollover is None；调用方自行使用 utils.rollover 默认
        p = _write_yaml(
            tmp_path,
            """
products:
  RB:
    name: 螺纹钢
    exchange: SHFE
    symbol_pattern: '^rb\\d{3,4}$'
    size: 10
    pricetick: 1.0
""",
        )
        reg = ProductRegistry.load(p)
        assert reg.get("RB").rollover is None

    def test_custom_rollover_parsed(self, tmp_path: Path) -> None:
        p = _write_yaml(
            tmp_path,
            """
products:
  AG:
    name: 白银
    exchange: SHFE
    symbol_pattern: '^ag\\d{3,4}$'
    size: 15
    pricetick: 1.0
    rollover:
      oi_pct_threshold: 15.0
      gap_floor_pct: 0.5
""",
        )
        reg = ProductRegistry.load(p)
        cfg = reg.get("AG").rollover
        assert isinstance(cfg, RolloverConfig)
        assert cfg.oi_pct_threshold == 15.0
        assert cfg.gap_floor_pct == 0.5


class TestDefaultSingleton:
    def test_get_default_registry_caches(self) -> None:
        a = get_default_registry()
        b = get_default_registry()
        assert a is b  # 懒加载缓存

    def test_set_default_registry_injects(self) -> None:
        custom = ProductRegistry(
            {
                "XX": Product(
                    code="XX",
                    name="Test",
                    exchange="SHFE",
                    symbol_pattern=__import__("re").compile(r"^xx\d+$"),
                    size=1.0,
                    pricetick=1.0,
                    rollover=None,
                    pattern_raw=r"^xx\d+$",
                )
            }
        )
        set_default_registry(custom)
        assert get_default_registry() is custom
        assert get_default_registry().underlying_of("xx99.SHFE") == "XX"
