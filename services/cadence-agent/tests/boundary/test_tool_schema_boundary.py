"""工具 schema 的边界测试。

这一层的 bug 不会在单元测试里炸，但会让真实模型**永远调不对**工具：
schema 少一个 required 字段，模型只能猜；schema 多一个不存在的字段，模型照着填。
"""

from __future__ import annotations

import json

import pytest

from cadence.guard.permissions import FORBIDDEN_TOOLS
from cadence.tools.base import Tool
from cadence.tools.registry import ToolRegistry, build_default_registry, provider_name, tool_schema


@pytest.fixture
def registry():
    return build_default_registry()


def test_openai_tools_is_cached_but_invalidated_by_register() -> None:
    """L3：schema 生成是纯 CPU（10 个 model，每次 5~15ms），每次 LLM 调用都重建会把
    事件循环堵成串行。缓存必须在注册新工具时失效，否则动态注册（测试/插件）看不到。"""
    async def handler(_args, _ctx):  # pragma: no cover - 只用来占位
        return None

    reg = ToolRegistry()
    reg.register(Tool(name="a.one", description="x", handler=handler))
    first = reg.openai_tools()
    assert first is reg.openai_tools(), "同一工具集应复用缓存"

    reg.register(Tool(name="a.two", description="y", handler=handler))
    refreshed = reg.openai_tools()
    assert refreshed is not first
    assert len(refreshed) == 2


def schema(registry, name: str) -> dict:
    return next(t for t in registry.openai_tools() if t["function"]["name"] == provider_name(name))


def test_exposes_ten_tools(registry) -> None:
    # 12 个注册，2 个 SP3 桩不暴露
    assert len(registry) == 12
    assert len(registry.openai_tools()) == 10
    assert {t["function"]["name"] for t in registry.openai_tools()} == {
        "task_search",
        "task_create",
        "task_update",
        "task_start",
        "task_complete",
        "task_delete",
        "task_split",
        "schedule_busy",
        "meta_clarify",
        "meta_finish",
    }


def test_all_tools_are_valid_provider_names(registry) -> None:
    for t in registry.openai_tools():
        name = t["function"]["name"]
        assert name.replace("_", "").replace("-", "").isalnum()
        assert "." not in name


def test_forbidden_tools_never_registered(registry) -> None:
    for forbidden in FORBIDDEN_TOOLS:
        assert forbidden not in registry
        assert all(t["function"]["name"] != provider_name(forbidden) for t in registry.openai_tools())


def test_task_create_title_property_present(registry) -> None:
    """BUG-SP2-01：_strip_titles 会删掉所有叫 title 的键，包括真正的字段。"""
    params = schema(registry, "task.create")["function"]["parameters"]
    assert "title" in params.get("properties", {}), (
        "task.create 的 title 属性被 _strip_titles 误删了，"
        "但 required 里仍然有 title —— provider 会收到一个自相矛盾的 schema"
    )


def test_task_split_child_title_property_present(registry) -> None:
    """BUG-SP2-01 的连带影响：嵌套 ChildTask 的 title 也被删。"""
    params = schema(registry, "task.split")["function"]["parameters"]
    child = params["$defs"]["ChildTask"]
    assert "title" in child.get("properties", {})


def test_non_title_fields_are_preserved(registry) -> None:
    params = schema(registry, "task.create")["function"]["parameters"]
    for field in ("description", "priority", "due_at", "estimate_min", "parent_id", "tags"):
        assert field in params.get("properties", {}), field


def test_schema_is_json_serializable(registry) -> None:
    for t in registry.openai_tools():
        json.dumps(t, ensure_ascii=False)


def test_delete_and_split_descriptions_contain_taboos(registry) -> None:
    delete = schema(registry, "task.delete")["function"]["description"]
    split = schema(registry, "task.split")["function"]["description"]
    update = schema(registry, "task.update")["function"]["description"]
    start = schema(registry, "task.start")["function"]["description"]
    assert "禁止" in delete and "确认" in delete
    assert "7" in split and "重新分解" in split
    # L1-4：三个写操作都要有一句「不要做什么」
    assert "禁忌" in delete and "禁忌" in split and "禁忌" in update
    # L1-2：取消不支持已经写进 task.update 的描述
    assert "status" in update and "meta.clarify" in update
    # L4.5：开始任务是独立工具，描述里写清 already DONE/CANCELLED 与重复调用
    assert "DONE" in start and "CANCELLED" in start and "不要重复调用" in start


def test_every_required_field_is_declared(registry) -> None:
    """L1-3：required 里的每个字段都必须出现在 properties 里，否则 provider 收到自相矛盾的 schema。"""
    for t in registry.openai_tools():
        params = t["function"]["parameters"]
        required = set(params.get("required", []))
        properties = set(params.get("properties", {}))
        missing = required - properties
        assert not missing, (
            f"{t['function']['name']} 的 required 字段没有在 properties 里声明：{sorted(missing)}"
        )


def test_defaults_are_explicit(registry) -> None:
    params = schema(registry, "task.create")["function"]["parameters"]
    assert params["properties"]["priority"]["default"] == "normal"
    # TaskSearchArgs.limit 默认 10；TaskSplitArgs.dry_run 默认 True
    search = schema(registry, "task.search")["function"]["parameters"]
    assert search["properties"]["limit"]["default"] == 10
    split = schema(registry, "task.split")["function"]["parameters"]
    assert split["properties"]["dry_run"]["default"] is True


def test_schema_size_regression_guard(registry) -> None:
    # 防止某次改动把 schema 撑到不可接受；具体预算见下一条
    assert registry.schema_token_estimate() < 3500


@pytest.mark.xfail(strict=True, reason="BUG-SP2-31：PRD 给工具 schema 的预算是 700 token，"
                                       "实测粗估约 2500（10 个工具），会挤占上下文预算")
def test_schema_size_within_prd_budget(registry) -> None:
    assert registry.schema_token_estimate() <= 700


def test_plan_tools_are_registered_but_not_exposed(registry) -> None:
    assert "plan.reschedule" in registry
    assert "plan.apply" in registry
    exposed = {t["function"]["name"] for t in registry.openai_tools()}
    assert "plan_reschedule" not in exposed
    assert "plan_apply" not in exposed


def test_tool_schema_keeps_required_fields(registry) -> None:
    for name, required in {
        "task.create": {"title"},
        "task.update": {"task_id", "version", "patch"},
        "task.start": {"task_id", "version"},
        "task.complete": {"task_id", "version"},
        "task.delete": {"task_id"},
        "task.split": {"children"},
        "meta.finish": {"message"},
        "meta.clarify": {"question"},
    }.items():
        params = schema(registry, name)["function"]["parameters"]
        assert required.issubset(set(params.get("required", []))), name
