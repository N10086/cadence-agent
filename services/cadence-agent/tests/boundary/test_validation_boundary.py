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
    _patch_body,
)


@pytest.mark.xfail(strict=True, reason="BUG-SP2-02：Core 的新任务 version=0，但 schema 要求 version>=1")
def test_version_zero_is_valid_for_update_and_complete() -> None:
    parse_args("task.update", TaskUpdateArgs, {"task_id": "t_001", "version": 0, "patch": {"title": "x"}})
    parse_args("task.complete", TaskCompleteArgs, {"task_id": "t_001", "version": 0})


def test_current_version_zero_rejected_for_update() -> None:
    """锁定 BUG-SP2-02 的当前行为：version 0 被参数校验拒绝。"""
    with pytest.raises(ToolError) as ei:
        parse_args("task.update", TaskUpdateArgs, {"task_id": "t_001", "version": 0, "patch": {"title": "x"}})
    assert ei.value.code is ErrorCode.VALIDATION_ERROR
    assert "version" in ei.value.message


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


def test_unknown_fields_are_silently_dropped() -> None:
    """BUG-SP2-09：parse_args 的第二次校验会剥掉未知字段，而不是报错。"""
    parsed = parse_args(
        "task.create",
        TaskCreateArgs,
        {"title": "x", "due": "2026-11-10T09:00:00+08:00"},  # 拼错的 due
    )
    assert parsed.due_at is None
    assert not hasattr(parsed, "due")


@pytest.mark.xfail(strict=True, reason="BUG-SP2-09：拼错的字段名被静默丢弃，任务会以'没有截止时间'创建成功，"
                                       "模型却以为设置成功了")
def test_bug_typo_field_should_not_be_silently_dropped() -> None:
    with pytest.raises(ToolError):
        parse_args("task.create", TaskCreateArgs, {"title": "x", "due": "2026-11-10T09:00:00+08:00"})


def test_mixed_unknown_and_known_patch_silently_drops_unknown() -> None:
    """BUG-SP2-10：patch 中未知键与合法键混在一起时，未知键静默消失，调用方收到成功。"""
    body = _patch_body({"title": "new", "stauts": "DONE", "estimate": 60})
    assert body == {"title": "new"}


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
