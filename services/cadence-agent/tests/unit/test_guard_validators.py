"""护栏：参数校验 + id 白名单 + 权限矩阵。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from cadence.guard import permissions, validators
from cadence.runtime.errors import ErrorCode, ToolError


class DemoArgs(BaseModel):
    task_id: str = Field(..., description="id")
    version: int = Field(..., ge=1)
    note: str | None = None


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def test_parse_ok() -> None:
    parsed = validators.parse_args("demo", DemoArgs, {"task_id": "t_001", "version": 2})
    assert parsed.task_id == "t_001"


def test_missing_required_raises_validation_error() -> None:
    with pytest.raises(ToolError) as exc:
        validators.parse_args("demo", DemoArgs, {"task_id": "t_001"})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR
    assert "version" in (exc.value.details or {}).get("missing", [])


def test_missing_args_produces_clarify_suggestion() -> None:
    """失败矩阵 F2：参数缺失不当作错误，而是当作歧义 → 去问用户。"""
    with pytest.raises(ToolError) as exc:
        validators.parse_args("demo", DemoArgs, {})
    suggestions = exc.value.suggestions
    assert suggestions and suggestions[0]["tool"] == "meta.clarify"


def test_unknown_keys_are_dropped() -> None:
    parsed = validators.parse_args("demo", DemoArgs, {"task_id": "t_001", "version": 1, "oops": 1})
    assert not hasattr(parsed, "oops")


# ---------------------------------------------------------------------------
# id 白名单
# ---------------------------------------------------------------------------


def test_collect_ids_walks_nested() -> None:
    args = {"task_id": "t_1", "patch": {"parent_id": "t_2"}, "items": [{"id": "t_3"}]}
    assert sorted(validators.collect_ids(args)) == ["t_1", "t_2", "t_3"]


def test_whitelist_accepts_visible() -> None:
    validators.assert_ids_visible({"task_id": "t_001"}, {"t_001"}, tool_name="task.update")


def test_whitelist_rejects_hallucination() -> None:
    with pytest.raises(ToolError) as exc:
        validators.assert_ids_visible({"task_id": "t_999"}, {"t_001"}, tool_name="task.update")
    assert exc.value.code is ErrorCode.TASK_ID_NOT_IN_CONTEXT
    assert exc.value.recoverable is True, "白名单拒绝是可恢复的：让模型重新 search"
    assert exc.value.suggestions[0]["tool"] == "task.search"


def test_whitelist_rejects_nested_id() -> None:
    with pytest.raises(ToolError):
        validators.assert_ids_visible({"patch": {"parent_id": "t_999"}}, {"t_001"}, tool_name="task.update")


def test_remember_ids_extracts_from_results() -> None:
    visible: set[str] = set()
    added = validators.remember_ids(visible, {"items": [{"id": "t_001"}, {"id": "t_002"}], "total": 2})
    assert added == {"t_001", "t_002"}
    assert visible == {"t_001", "t_002"}


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------


def test_destructive_tools_exist() -> None:
    assert permissions.permission_of("task.delete") is permissions.Permission.DESTRUCTIVE
    assert permissions.permission_of("task.search") is permissions.Permission.READ


def test_destructive_always_needs_confirm() -> None:
    assert permissions.needs_confirm("task.delete") is True
    assert permissions.needs_confirm("plan.apply") is True


def test_batch_over_threshold_needs_confirm() -> None:
    assert permissions.needs_confirm("task.split", affected=4) is True
    assert permissions.needs_confirm("task.split", affected=3) is False


def test_read_tools_need_no_confirm() -> None:
    assert permissions.needs_confirm("task.search") is False


def test_forbidden_tools_are_blocked() -> None:
    for name in permissions.FORBIDDEN_TOOLS:
        assert permissions.is_forbidden(name)
        with pytest.raises(permissions.PermissionDenied):
            permissions.assert_executable(name)


def test_no_forbidden_tool_is_registered() -> None:
    """反向验收：禁止清单里的工具，一个都不许出现在注册表里。"""
    from cadence.tools.registry import build_default_registry

    registry = build_default_registry()
    assert not (set(registry.names()) & set(permissions.FORBIDDEN_TOOLS))
