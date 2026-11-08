"""工具类别路由的单测：规则映射、解析、按类过滤、control 兜底。"""

from __future__ import annotations

from cadence.tools.registry import build_default_registry
from cadence.tools.routing import (
    ALL_CATEGORIES,
    CATEGORY_SUMMARIES,
    categories_for_intent,
    merge_categories,
    normalize,
    parse_categories,
    tools_for_categories,
)


def test_four_categories_are_the_permission_values() -> None:
    assert set(ALL_CATEGORIES) == {"read", "write", "destructive", "control"}
    assert set(CATEGORY_SUMMARIES) == set(ALL_CATEGORIES)
    assert all(CATEGORY_SUMMARIES.values()), "每个类别都要有摘要"


def test_intent_to_categories() -> None:
    assert categories_for_intent("query") == ["read"]
    assert categories_for_intent("mutate") == ["read", "write"]
    assert categories_for_intent("multi") == ["read", "write"]
    assert categories_for_intent("destructive") == ["read", "destructive"]
    assert categories_for_intent("chitchat") == []
    assert set(categories_for_intent("unknown")) == set(ALL_CATEGORIES)


def test_parse_categories_accepts_json_and_noise() -> None:
    assert parse_categories('{"categories":["read","write"]}') == ["read", "write"]
    assert parse_categories('```json\n{"categories": ["destructive"]}\n```') == ["destructive"]
    assert parse_categories('我觉得需要 read 和 write') == ["read", "write"]
    assert parse_categories("") == []
    assert parse_categories("完全看不懂") == []


def test_parse_categories_drops_unknown_names() -> None:
    assert parse_categories('{"categories":["read","sql.execute"]}') == ["read"]


def test_normalize_always_keeps_control() -> None:
    assert normalize([]) == ["control"]
    assert normalize(["read"]) == ["read", "control"]
    assert normalize(["read", "read", "control"]) == ["read", "control"]


def test_merge_falls_back_to_all_when_only_control() -> None:
    # 实测病态：把"新建一个任务"判成 control，模型连 create 都拿不到 → 必须退回全量
    assert merge_categories(["control"], "mutate") == list(ALL_CATEGORIES)
    assert merge_categories([], "query") == list(ALL_CATEGORIES)


def test_merge_unions_with_intent_rules() -> None:
    # 模型只给 read，但意图是 mutate → 补上 write
    assert merge_categories(["read"], "mutate") == ["read", "write"]
    # 模型给 write，意图是 query → 保留 write（模型可能看到了额外信号）
    assert merge_categories(["write"], "query") == ["write", "read"]


def test_registry_filters_tools_by_category() -> None:
    registry = build_default_registry()

    read_only = {t.name for t in registry.tools_for(["read"])}
    assert read_only == {"task.search", "schedule.busy"}

    read_write = {t.name for t in registry.tools_for(["read", "write"])}
    assert read_write == {
        "task.search", "schedule.busy",
        "task.create", "task.update", "task.start", "task.complete", "task.split",
    }

    control = {t.name for t in registry.tools_for(["control"])}
    assert control == {"meta.clarify", "meta.finish"}

    # plan.* 虽然注册了，但 exposed=False，任何类别都不该出现
    assert all(not name.startswith("plan.") for name in read_write | control)


def test_openai_tools_filtered_and_cached_per_category_set() -> None:
    registry = build_default_registry()
    a = registry.openai_tools(["read", "control"])
    b = registry.openai_tools(["control", "read"])  # 顺序不同，同一份缓存
    assert a is b
    names = {t["function"]["name"] for t in a}
    assert names == {"task_search", "schedule_busy", "meta_clarify", "meta_finish"}
    assert len(a) < len(registry.openai_tools()), "分类后必须比全量小"


def test_tool_schemas_in_subset_keep_their_descriptions() -> None:
    """第二级注入的每个工具都要带自己的 description（类别摘要不进来）。"""
    registry = build_default_registry()
    tools = registry.openai_tools(["write", "control"])
    assert tools
    assert all(t["function"]["description"] for t in tools)
    descriptions = " ".join(t["function"]["description"] for t in tools)
    for summary in CATEGORY_SUMMARIES.values():
        assert summary not in descriptions, "类别摘要不应出现在第二级工具描述里"
