"""产品注册表：把 ``vt_symbol`` 拆成 (underlying, exchange) 的单一事实源。

为什么独立成模块
----------------
原先代码里 ``vt_symbol="rb2410.SHFE"`` 既代表了"螺纹钢"这个标的（策略 alpha 的
对象），又代表了"2024 年 10 月主力合约"这个执行端工具。两个生命周期被压扁到
同一个字符串里：
- 策略关心的是 RB 的 alpha，跟 2410 还是 2501 无关
- 风控应当按 *标的* 限额（同标的多个月份持仓应当汇总），而不是按合约
- 换月时合约换了，"持仓"应当语义上延续

P1 引入 ProductRegistry 解决"标的 ↔ 合约"双向解析，先服务 RiskGuard 的
``max_position_per_underlying``；P3 再扩展主力合约表 + RolloverEvent 广播。

设计选择
--------
- ``symbol_pattern`` 用正则匹配 vt_symbol 的 symbol 段（点号前段），不直接用
  前缀 —— 因为 ``ag2506`` 和 ``a2501``（豆一）都以 'a' 开头，必须靠尾部 4 位
  数字限制 boundary。
- 未注册的 vt_symbol → ``KeyError``（fail-fast）。回测/研究里只要碰到都得在
  YAML 里登记一笔，等价于"承认这是个 first-class 产品"。
- ProductRegistry 实例无状态、可冻结、纯解析：因此安全做成进程级单例（``get_default_registry``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # type: ignore[import-untyped]

DEFAULT_PRODUCTS_YAML = Path(__file__).resolve().parent.parent / "config" / "products.yaml"


@dataclass(frozen=True)
class RolloverConfig:
    """H1.5 阈值（per-underlying 可覆盖 utils.rollover 全局默认）。"""

    oi_pct_threshold: float
    gap_floor_pct: float


@dataclass(frozen=True)
class Product:
    """一个 underlying 的 immutable 元数据。"""

    code: str  # "RB"
    name: str  # "螺纹钢"
    exchange: str  # "SHFE"
    symbol_pattern: re.Pattern[str]
    size: float
    pricetick: float
    rollover: RolloverConfig | None = None
    # The raw regex string is kept around so error messages can quote what the
    # YAML actually declared rather than the compiled object's repr.
    pattern_raw: str = field(default="")


class ProductRegistry:
    """产品注册表，从 YAML 加载，提供双向解析。"""

    def __init__(self, products: dict[str, Product]) -> None:
        # 拒绝重复 code（YAML 自身会去重，但程序化构造也走这条路径）
        self._by_code: dict[str, Product] = dict(products)
        # 反查表：(exchange, symbol_pattern) → code。按声明顺序遍历，第一个匹配胜出
        self._search_order: list[Product] = list(products.values())

    @classmethod
    def load(cls, path: Path | str | None = None) -> ProductRegistry:
        """从 YAML 加载。schema 错误立即抛 ValueError 并定位到具体 entry。"""
        p = Path(path) if path else DEFAULT_PRODUCTS_YAML
        if not p.exists():
            raise FileNotFoundError(f"products.yaml not found: {p}")

        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        if raw is None:
            return cls({})
        if not isinstance(raw, dict) or "products" not in raw:
            raise ValueError(f"{p}: top-level must be a mapping with a 'products' key")

        products_raw = raw["products"]
        if not isinstance(products_raw, dict):
            raise ValueError(f"{p}: 'products' must be a mapping of code → entry")

        products: dict[str, Product] = {}
        for code, entry in products_raw.items():
            if not isinstance(entry, dict):
                raise ValueError(f"{p}: product {code!r} entry must be a mapping")
            try:
                pattern_raw = str(entry["symbol_pattern"])
                product = Product(
                    code=str(code).upper(),
                    name=str(entry.get("name", code)),
                    exchange=str(entry["exchange"]),
                    symbol_pattern=re.compile(pattern_raw, re.IGNORECASE),
                    size=float(entry["size"]),
                    pricetick=float(entry["pricetick"]),
                    rollover=_parse_rollover(entry.get("rollover")),
                    pattern_raw=pattern_raw,
                )
            except KeyError as e:
                raise ValueError(f"{p}: product {code!r} missing field {e}") from None
            except re.error as e:
                raise ValueError(f"{p}: product {code!r} invalid symbol_pattern: {e}") from None
            products[product.code] = product

        return cls(products)

    # ------------------------------------------------------------------ public API

    def get(self, code: str) -> Product:
        """按 code 查产品；未注册 → KeyError。"""
        return self._by_code[code.upper()]

    def underlying_of(self, vt_symbol: str) -> str:
        """``rb2410.SHFE`` → ``RB``。未匹配 → KeyError，禁止悄悄漏过去。

        匹配规则：拆出 ``symbol`` 段（点号前），按 ProductRegistry 内声明顺序
        遍历，第一个 ``re.fullmatch(symbol)`` 成立且 exchange 一致的产品胜出。
        ``exchange`` 段允许大小写差异（CTP 大写、研究脚本可能小写）。
        """
        if "." not in vt_symbol:
            raise KeyError(f"vt_symbol {vt_symbol!r} 缺少 exchange 段（期望 'symbol.exchange'）")
        symbol, exchange = vt_symbol.split(".", 1)
        exchange = exchange.upper()
        for product in self._search_order:
            if product.exchange.upper() != exchange:
                continue
            if product.symbol_pattern.fullmatch(symbol):
                return product.code
        raise KeyError(
            f"vt_symbol {vt_symbol!r} 未在 ProductRegistry 中注册 — "
            f"请在 config/products.yaml 添加对应 product 条目"
        )

    def underlying_of_or_none(self, vt_symbol: str) -> str | None:
        """非 fail-fast 变体：未匹配返回 ``None``。

        RiskGuard 必须能容忍"未注册合约"—— 收到 EVENT_TRADE 时若 vt_symbol 没在
        注册表里，应当退化回 per-vt_symbol 限额而不是直接抛异常把熔断引擎掀翻。
        """
        try:
            return self.underlying_of(vt_symbol)
        except KeyError:
            return None

    def all_codes(self) -> list[str]:
        """所有注册的 underlying code，便于 dashboards / 巡检列表。"""
        return list(self._by_code.keys())


def _parse_rollover(raw: object) -> RolloverConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"rollover 必须是 mapping 或省略，得到 {type(raw).__name__}")
    return RolloverConfig(
        oi_pct_threshold=float(raw.get("oi_pct_threshold", 20.0)),
        gap_floor_pct=float(raw.get("gap_floor_pct", 0.3)),
    )


# 进程级默认实例 —— ProductRegistry 是只读注册表，安全共享
_default_registry: ProductRegistry | None = None


def get_default_registry() -> ProductRegistry:
    """获取（懒加载）默认 YAML 实例。RiskGuard / 仪表板按需调用。"""
    global _default_registry
    if _default_registry is None:
        _default_registry = ProductRegistry.load()
    return _default_registry


def set_default_registry(registry: ProductRegistry | None) -> None:
    """测试 / 自定义路径下注入。``None`` 触发下次调用时重新懒加载。"""
    global _default_registry
    _default_registry = registry
