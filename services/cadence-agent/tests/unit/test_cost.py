"""成本换算与 schema 体积。

成本是评测指标（NFR-3 单轮 ≤ ¥0.01），所以定价必须可配、可测，
不能在代码里写死一个会悄悄失真的数字。
"""

from __future__ import annotations

from cadence.config import Settings
from cadence.llm.cost import Pricing, cost_of, estimate_tokens, pricing_for


def make_settings(**kw) -> Settings:
    base = dict(
        llm_mode="fake",
        deepseek_api_key="",
        deepseek_price_in_per_m=2.0,
        deepseek_price_out_per_m=8.0,
    )
    base.update(kw)
    return Settings(**base)


def test_fake_model_is_free() -> None:
    settings = make_settings()
    assert cost_of(settings, "fake-model", 10_000, 10_000) == 0.0


def test_deepseek_pricing_math() -> None:
    settings = make_settings()
    # 1M in + 1M out = 2 + 8 = 10 元
    assert cost_of(settings, "deepseek-chat", 1_000_000, 1_000_000) == 10.0


def test_single_step_turn_under_target() -> None:
    """NFR-3：单轮 ≤ ¥0.01。fast 路径 = 2000 in + 200 out。"""
    settings = make_settings()
    cost = cost_of(settings, "deepseek-chat", 2000, 200)
    assert cost < 0.01, f"fast 路径成本 ¥{cost:.4f} 超过 ¥0.01"


def test_multi_step_turn_is_expensive_and_that_is_expected() -> None:
    """3 步 ≈ 3 倍上下文 → 必然超 ¥0.01。这是产品事实，不是 bug，要如实记录。"""
    settings = make_settings()
    cost = cost_of(settings, "deepseek-chat", 6000, 600)
    assert cost > 0.01


def test_pricing_is_configurable() -> None:
    cheap = make_settings(deepseek_price_in_per_m=0.0, deepseek_price_out_per_m=0.0)
    assert cost_of(cheap, "deepseek-chat", 10**6, 10**6) == 0.0


def test_zhipu_is_free_tier() -> None:
    settings = make_settings(zhipu_price_per_m=0.0)
    assert pricing_for(settings, "glm-4-flash") == Pricing(0.0, 0.0)


def test_estimate_tokens_roughly_sane() -> None:
    assert estimate_tokens("") == 0
    assert 1 <= estimate_tokens("你好世界") <= 8
    assert estimate_tokens("a" * 40) >= 5


def test_tool_schema_stays_within_budget() -> None:
    """schema 体积是有预算的：它每轮都要发一次，超了就是每轮都多花钱。"""
    from cadence.tools.registry import build_default_registry

    registry = build_default_registry()
    estimate = registry.schema_token_estimate()
    assert len(registry) == 11, "契约要求 11 个工具"
    assert len(registry.exposed()) == 9, "SP2 只暴露 9 个（plan.* 是 SP3 的桩）"
    assert estimate < 2600, f"schema 约 {estimate} token，超出预算"
