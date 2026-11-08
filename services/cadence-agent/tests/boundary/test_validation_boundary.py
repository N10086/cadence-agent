"""参数校验与 id 白名单的边界测试。

这里的每一条都对应 PRD 里的一句承诺：
- P2：id 只能来自本轮可见集合；
- P4：错误必须可行动；
- 失败矩阵 F2：参数缺失 → clarify，而不是硬猜。
"""

from __future__ import annotations

import pytest

from cadence.guard.validators import (
    ID_KEYS,
    assert_ids_visible,
    collect_ids,
    parse_args,
    remember_ids,
)
from cadence.runtime.errors import ErrorCode, ToolError
from cadence.tools.task_tools import (
    TaskCompleteArgs,
    TaskCreateArgs,
    TaskDeleteArgs,
    TaskSearchArgs,
    TaskSplitArgs,
    TaskUpdateArgs,
)


def test_version_zero_is_valid_for_update_and_complete() -> None:
    parse_args("task.update", TaskUpdateArgs, {"task_id": "t_001", "version": 0, "patch": {"title": "x"}})
    parse_args("task.complete", TaskCompleteArgs, {"task_id": "t_001", "version": 0})


def test_version_one_is_accepted() -> None:
    m = parse_args("task.update", TaskUpdateArgs, {"task_id": "t_001", "version": 1, "patch": {"title": "x"}})
    assert m.version == 1


def test_missing_required_reports_missing_fields() -> None:
    with pytest.raises(ToolError) as ei:
        parse_args("task.delete", TaskDeleteArgs, {})
    assert ei.value.code is ErrorCode.VALIDATION_ERROR
    assert "task_id" in ei.value.message
    assert ei.value.details["missing"] == ["task_id"]
    # 参数缺失应被当作歧义 → 给 meta.clarify 的建议
    assert ei.value.suggestions and ei.value.suggestions[0]["tool"] == "meta.clarify"


def test_invalid_enum_and_range_rejected() -> None:
    with pytest.raises(ToolError):
        parse_args("task.search", TaskSearchArgs, {"status": "BLOCKED"})
    with pytest.raises(ToolError):
        parse_args("task.create", TaskCreateArgs, {"title": "x", "estimate_min": 4})
    with pytest.raises(ToolError):
        parse_args("task.create", TaskCreateArgs, {"title": "x", "estimate_min": 481})
    with pytest.raises(ToolError):
        parse_args("task.search", TaskSearchArgs, {"limit": 0})
    with pytest.raises(ToolError):
        parse_args("task.search", TaskSearchArgs, {"limit": 21})


def test_unknown_fields_are_rejected() -> None:
    """L1-1：顶层未知字段必须返回 VALIDATION_ERROR，而不是被静默剥掉。"""
    with pytest.raises(ToolError) as ei:
        parse_args(
            "task.create",
            TaskCreateArgs,
            {"title": "x", "due": "2026-11-10T09:00:00+08:00"},  # 拼错的 due
        )
    assert ei.value.code is ErrorCode.VALIDATION_ERROR
    assert ei.value.details["unknown"] == ["due"]
    assert "due" in ei.value.message


def test_typo_field_returns_suggestion() -> None:
    """拼错的字段名要给出最接近的合法字段（编辑距离 <=2 且同前缀）。"""
    with pytest.raises(ToolError) as ei:
        parse_args("task.create", TaskCreateArgs, {"title": "x", "due": "2026-11-10T09:00:00+08:00"})
    assert ei.value.suggestions == [{"field": "due_at", "reason": "字段 due 不存在"}]
    assert "due_at" in ei.value.message


def test_mixed_unknown_patch_is_rejected() -> None:
    """顶层已知参数（patch）与未知参数混在一起时，未知参数同样要拒绝。"""
    with pytest.raises(ToolError) as ei:
        parse_args(
            "task.update",
            TaskUpdateArgs,
            {
                "task_id": "t_001",
                "version": 0,
                "patch": {"title": "new"},
                "stauts": "DONE",  # 顶层拼错，不是 patch 内部
            },
        )
    assert ei.value.code is ErrorCode.VALIDATION_ERROR
    assert ei.value.details["unknown"] == ["stauts"]

    # patch 内部的键由 Core 校验：parse_args 不能因为里面出现 status 就拒掉
    parsed = parse_args(
        "task.update",
        TaskUpdateArgs,
        {"task_id": "t_001", "version": 0, "patch": {"due_at": "2026-11-10T09:00:00+08:00", "status": "CANCELLED"}},
    )
    assert parsed.patch["status"] == "CANCELLED"


def test_collect_ids_recurses_and_only_uses_id_keys() -> None:
    args = {
        "task_id": "a",
        "nested": {"id": "b"},
        "ids": ["c"],
        "depends_on": [0, 1],  # 子任务下标，不是 task id
        "other": "d",
        "parent_id": None,
    }
    assert set(collect_ids(args)) == {"a", "b", "c"}
    for key in ("task_id", "parent_id", "depends_on_id", "id", "ids", "depends_on", "pinned_task_ids"):
        assert key in ID_KEYS


def test_whitelist_blocks_hallucinated_id() -> None:
    with pytest.raises(ToolError) as ei:
        assert_ids_visible({"task_id": "t_999"}, {"t_001"}, tool_name="task.complete")
    assert ei.value.code is ErrorCode.TASK_ID_NOT_IN_CONTEXT
    assert ei.value.details["offending_id"] == "t_999"


def test_whitelist_allows_exact_id_and_empty_args() -> None:
    assert_ids_visible({"task_id": "t_001"}, {"t_001"}, tool_name="task.complete")
    assert_ids_visible({}, {"t_001"}, tool_name="task.search")


def test_remember_ids_adds_new_ids_and_versions() -> None:
    visible: set[str] = set()
    remember_ids(visible, {"items": [{"id": "t_1", "version": 3}], "taskId": "t_2"})
    assert visible == {"t_1", "t_2"}


def test_task_split_children_not_treated_as_task_ids() -> None:
    # children[].depends_on 是下标；如果被误当 id，白名单会错误拦截
    raw = {
        "children": [
            {"title": "a", "depends_on": [1]},
            {"title": "b", "depends_on": []},
        ]
    }
    parsed = parse_args("task.split", TaskSplitArgs, raw)
    assert collect_ids(parsed.model_dump()) == []


def test_none_args_model_currently_crashes() -> None:
    """BUG-SP2-32：model_cls=None 时 parse_args 会 BaseModel() 直接实例化，抛 PydanticUserError。
    当前所有注册工具都有 args_model，所以不可达；但它是明确的兜底分支 bug。"""
    from pydantic.errors import PydanticUserError

    with pytest.raises(PydanticUserError):
        parse_args("no.args", None, {"anything": 1})


def test_error_payload_is_actionable() -> None:
    with pytest.raises(ToolError) as ei:
        parse_args("task.delete", TaskDeleteArgs, {})
    payload = ei.value.to_tool_json()
    assert payload["ok"] is False
    assert payload["error"]["recoverable"] is True
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert payload["error"]["hint"]
