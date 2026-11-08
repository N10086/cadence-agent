"""确定性路由。"""

from __future__ import annotations

import pytest

from cadence.llm.router import Tier, route


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("看看我今天有什么任务", Tier.FAST),
        ("列出所有任务", Tier.FAST),
        ("把买牛奶标记完成", Tier.FAST),
        ("把交季度报告删掉", Tier.REASON),  # 破坏性 → 值得更强的理解
        ("把买牛奶标记完成，然后建一个周五前交报告的高优先级任务", Tier.REASON),
        ("我这周发烧，周三到周五干不了活，帮我重排一下", Tier.REASON),
        ("顺便把周报也挪到下周一", Tier.REASON),
    ],
)
def test_route_tiers(text: str, expected: Tier) -> None:
    assert route(text).tier is expected


def test_route_is_deterministic() -> None:
    text = "把买牛奶标记完成，然后建一个周五交报告的任务"
    first = route(text)
    for _ in range(5):
        again = route(text)
        assert again.tier is first.tier
        assert again.rule == first.rule


def test_empty_message_is_fast() -> None:
    decision = route("   ")
    assert decision.tier is Tier.FAST
    assert decision.rule == "empty"


def test_reason_always_explains_itself() -> None:
    """每条决策都要能回答"为什么"——不然改 prompt 时没法归因。"""
    for text in ["看看任务", "完成它", "帮我重排这周", ""]:
        decision = route(text)
        assert decision.reason
        assert decision.rule


def test_long_text_falls_back_to_reason() -> None:
    text = "嗯" * 80
    assert route(text).tier is Tier.REASON
    assert route(text).rule == "long-text"
