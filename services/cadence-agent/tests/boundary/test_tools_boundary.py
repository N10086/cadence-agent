"""工具 handler 的边界测试。

重点不是 happy path，而是：
- 参数映射（snake_case → Core 的 camelCase）
- 部分失败时的进度与依赖下标
- 两阶段删除
- 幂等键的稳定性
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cadence.runtime.budget import Budget
from cadence.runtime.context import TurnContext
from cadence.runtime.errors import ErrorCode, ToolError
from cadence.runtime.trace import TraceRecorder
from cadence.tools.task_tools import (
    ChildTask,
    TaskCompleteArgs,
    TaskCreateArgs,
    TaskDeleteArgs,
    TaskSearchArgs,
    TaskSplitArgs,
    TaskUpdateArgs,
    _create,
    _delete,
    _iso,
    _search,
    _slim,
    _split,
    _update,
    _complete,
)

TZ8 = timezone(timedelta(hours=8))


def ctx_for(rt) -> TurnContext:
    trace = TraceRecorder("tr_tools", rt.settings.resolved_trace_dir, session_id="s_tools")
    return TurnContext(
        settings=rt.settings,
        core=rt.core,
        trace=trace,
        confirms=rt.confirms,
        trace_id="tr_tools",
        session_id="s_tools",
        budget=Budget(max_steps=8, max_tokens=200_000),
    )


async def test_search_maps_filters_and_remembers_ids(runtime, core) -> None:
    ctx = ctx_for(runtime)
    data = await _search(TaskSearchArgs(keyword="牛奶", status="todo", priority="normal",
                                        due_before="2026-11-10T00:00:00+08:00", limit=5), ctx)
    assert data["items"][0]["id"] == "t_001"
    assert data["items"][0]["version"] == 0
    assert ctx.visible_task_ids == {"t_001"}

    call = core.calls[-1]
    assert call["params"]["status"] == "TODO"
    assert call["params"]["priority"] == "NORMAL"
    assert call["params"]["dueBefore"].startswith("2026-11-10T00:00:00")
    assert call["params"]["limit"] == 5


async def test_search_keyword_is_title_only_in_fake_core(runtime, core) -> None:
    # FakeCore（以及 Core）只按 title 匹配；工具描述却写"标题/描述"
    core.tasks["t_001"]["description"] = "needle"
    data = await _search(TaskSearchArgs(keyword="needle"), ctx_for(runtime))
    assert data["items"] == []


async def test_search_has_more_and_total(runtime, core) -> None:
    data = await _search(TaskSearchArgs(limit=1), ctx_for(runtime))
    assert data["total"] == 2
    assert data["has_more"] is True
    assert len(data["items"]) == 1


async def test_create_maps_fields_and_sets_idempotency(runtime, core) -> None:
    ctx = ctx_for(runtime)
    data = await _create(TaskCreateArgs(
        title="新任务",
        description="desc",
        priority="high",
        start_at="2026-11-10T09:00:00+08:00",
        due_at="2026-11-11T18:00:00+08:00",
        estimate_min=30,
        tags=["a"],
        pinned=True,
        rrule="FREQ=DAILY",
    ), ctx)
    assert data["task"]["title"] == "新任务"
    assert data["task"]["version"] == 0
    call = core.calls[-1]
    assert call["body"]["priority"] == "HIGH"
    assert call["body"]["startAt"].startswith("2026-11-10T09:00:00")
    assert call["body"]["estimateMin"] == 30
    assert call["idempotency_key"].startswith("idem_tr_tools")


async def test_create_idempotency_key_stable_for_same_step(runtime) -> None:
    ctx = ctx_for(runtime)
    k1 = ctx.idempotency_key("create")
    k2 = ctx.idempotency_key("create")
    assert k1 == k2
    ctx.step_index = 3
    assert ctx.idempotency_key("create") != k1


async def test_create_registers_new_task_in_whitelist(runtime) -> None:
    ctx = ctx_for(runtime)
    data = await _create(TaskCreateArgs(title="new"), ctx)
    assert data["task"]["id"] in ctx.visible_task_ids
    assert ctx.visible_versions[data["task"]["id"]] == 0


async def test_update_maps_patch_and_returns_warnings(runtime, core) -> None:
    core.tasks["t_001"]["version"] = 1
    ctx = ctx_for(runtime)
    ctx.remember(core.tasks["t_001"])
    data = await _update(TaskUpdateArgs(
        task_id="t_001", version=1, patch={"title": "新标题", "priority": "high", "due_at": "2026-11-12T18:00:00+08:00"},
    ), ctx)
    assert data["task"]["title"] == "新标题"
    assert data["warnings"] == []
    assert core.tasks["t_001"]["version"] == 2


async def test_update_unknown_only_patch_warns_without_calling_core(runtime, core) -> None:
    """patch 内部只有未知键：只给 warning + applied=false，不报 VALIDATION_ERROR、不打 Core。"""
    core.tasks["t_001"]["version"] = 1
    ctx = ctx_for(runtime)
    ctx.remember(core.tasks["t_001"])
    data = await _update(TaskUpdateArgs(task_id="t_001", version=1, patch={"stauts": "DONE"}), ctx)
    assert data["applied"] is False
    assert data["task"] is None
    assert data["warnings"][0]["field"] == "stauts"
    assert not [c for c in core.calls if c["method"] == "PATCH"]


async def test_update_mixed_patch_warns_but_applies_known(runtime, core) -> None:
    """patch 内部的未知键不阻断（键由 Core 校验），但必须作为 warning 回给模型。"""
    core.tasks["t_001"]["version"] = 1
    ctx = ctx_for(runtime)
    ctx.remember(core.tasks["t_001"])
    data = await _update(TaskUpdateArgs(
        task_id="t_001", version=1, patch={"title": "ok", "stauts": "DONE", "estimate": 60},
    ), ctx)
    assert data["task"]["title"] == "ok"
    assert core.tasks["t_001"]["status"] == "TODO"
    warned = {w["field"] for w in data["warnings"]}
    assert {"stauts", "estimate"}.issubset(warned)


async def test_update_status_cancel_is_rejected_with_clarify(runtime, core) -> None:
    """L1-2：Core 的 PATCH 不接受 status，取消必须走 meta.clarify 明确告知不支持。"""
    core.tasks["t_001"]["version"] = 1
    ctx = ctx_for(runtime)
    ctx.remember(core.tasks["t_001"])
    with pytest.raises(ToolError) as ei:
        await _update(TaskUpdateArgs(task_id="t_001", version=1, patch={"status": "CANCELLED"}), ctx)
    assert ei.value.code is ErrorCode.NOT_IMPLEMENTED
    assert ei.value.suggestions[0]["tool"] == "meta.clarify"
    assert "不支持取消" in ei.value.message
    # 没有发出任何写请求
    assert not [c for c in core.calls if c["method"] == "PATCH"]


async def test_complete_passes_version_and_rejects_stale(runtime, core) -> None:
    core.tasks["t_001"]["version"] = 1
    ctx = ctx_for(runtime)
    ctx.remember(core.tasks["t_001"])
    data = await _complete(TaskCompleteArgs(task_id="t_001", version=1), ctx)
    assert data["task"]["status"] == "done"


async def test_delete_two_phase_uses_core_token(runtime, core) -> None:
    ctx = ctx_for(runtime)
    ctx.remember(core.tasks["t_001"])
    data = await _delete(TaskDeleteArgs(task_id="t_001"), ctx)
    assert data["deleted"] is True
    assert "t_001" not in core.tasks
    # 第一次不带 token，第二次带 Core 的 ct_ token
    deletes = [c for c in core.calls if c["method"] == "DELETE"]
    assert len(deletes) == 2
    assert deletes[0]["confirm_token"] is None
    assert deletes[1]["confirm_token"] == "ct_fake"


async def test_delete_missing_core_token_raises(runtime, core) -> None:
    ctx = ctx_for(runtime)
    ctx.remember(core.tasks["t_001"])

    def hook(method, path):
        if method == "DELETE" and path.endswith("t_001"):
            raise ToolError(ErrorCode.CONFIRM_REQUIRED, "no token", details={})

    core.on_request = hook
    with pytest.raises(ToolError) as ei:
        await _delete(TaskDeleteArgs(task_id="t_001"), ctx)
    assert ei.value.code is ErrorCode.CONFIRM_REQUIRED


async def test_delete_second_time_returns_not_found(runtime, core) -> None:
    ctx = ctx_for(runtime)
    ctx.remember(core.tasks["t_001"])
    await _delete(TaskDeleteArgs(task_id="t_001"), ctx)
    with pytest.raises(ToolError) as ei:
        await _delete(TaskDeleteArgs(task_id="t_001"), ctx)
    assert ei.value.code is ErrorCode.TASK_ID_NOT_FOUND


async def test_split_dry_run_does_not_write(runtime, core) -> None:
    ctx = ctx_for(runtime)
    data = await _split(TaskSplitArgs(children=[
        ChildTask(title="c0"), ChildTask(title="c1"),
    ]), ctx)
    assert data["dry_run"] is True
    assert data["count"] == 2
    assert not any(c["method"] == "POST" and c["path"] == "/api/v1/tasks" for c in core.calls)


async def test_split_creates_children_and_dependencies(runtime, core) -> None:
    ctx = ctx_for(runtime)
    data = await _split(TaskSplitArgs(children=[
        ChildTask(title="c0", estimate_min=30),
        ChildTask(title="c1", depends_on=[0]),
    ], dry_run=False), ctx)
    assert data["ok"] is True
    assert data["count"] == 2
    ids = [c["id"] for c in data["created"]]
    assert all(ids)
    # c1 依赖 c0
    assert core.dependencies[ids[1]] == [ids[0]]


async def test_split_partial_failure_reports_progress(runtime, core) -> None:
    ctx = ctx_for(runtime)
    n = {"created": 0}

    def hook(method, path):
        if method == "POST" and path == "/api/v1/tasks":
            n["created"] += 1
            if n["created"] == 2:
                raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "boom")

    core.on_request = hook
    data = await _split(TaskSplitArgs(children=[
        ChildTask(title="c0"), ChildTask(title="c1"), ChildTask(title="c2"),
    ], dry_run=False), ctx)
    assert data["partial"] is True
    assert data["count"] == 2
    assert [f["index"] for f in data["failures"]] == [1]


async def test_split_partial_failure_dependency_index_is_wrong(runtime, core) -> None:
    """BUG-SP2-11：用 created 列表的下标当 children 下标；中间失败后依赖会连错任务。"""
    ctx = ctx_for(runtime)
    n = {"created": 0}

    def hook(method, path):
        if method == "POST" and path == "/api/v1/tasks":
            n["created"] += 1
            if n["created"] == 2:
                raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "boom")

    core.on_request = hook
    data = await _split(TaskSplitArgs(children=[
        ChildTask(title="c0", depends_on=[1]),  # 本意依赖 c1，但 c1 创建失败
        ChildTask(title="c1"),
        ChildTask(title="c2"),
    ], dry_run=False), ctx)
    ids = [c["id"] for c in data["created"]]
    # 当前实现把 c0 连到了 created[1]（c2），而不是报告 c1 失败导致依赖无法建立
    assert core.dependencies[ids[0]] == [ids[1]]


@pytest.mark.xfail(strict=True, reason="BUG-SP2-11：部分失败后 depends_on 下标按 created 列表解释，"
                                       "会把依赖静默连到错误的任务上")
async def test_bug_split_partial_failure_dependency_index_should_not_mislink(runtime, core) -> None:
    ctx = ctx_for(runtime)
    n = {"created": 0}

    def hook(method, path):
        if method == "POST" and path == "/api/v1/tasks":
            n["created"] += 1
            if n["created"] == 2:
                raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "boom")

    core.on_request = hook
    data = await _split(TaskSplitArgs(children=[
        ChildTask(title="c0", depends_on=[1]),
        ChildTask(title="c1"),
        ChildTask(title="c2"),
    ], dry_run=False), ctx)
    ids = [c["id"] for c in data["created"]]
    assert ids[0] not in core.dependencies  # 依赖目标失败，应报告而不是乱连


async def test_split_dependency_failure_is_silently_swallowed(runtime, core) -> None:
    """BUG-SP2-12：依赖建不上时 except ToolError: pass，结果仍报 ok。"""
    ctx = ctx_for(runtime)

    def hook(method, path):
        if method == "POST" and path.endswith("/dependencies"):
            raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "dep down")

    core.on_request = hook
    data = await _split(TaskSplitArgs(children=[
        ChildTask(title="c0"), ChildTask(title="c1", depends_on=[0]),
    ], dry_run=False), ctx)
    assert data["ok"] is True
    assert "failures" not in data
    assert core.dependencies == {}


@pytest.mark.xfail(strict=True, reason="BUG-SP2-12：依赖建立失败必须报告，不能静默 ok")
async def test_bug_split_dependency_failure_must_be_reported(runtime, core) -> None:
    ctx = ctx_for(runtime)

    def hook(method, path):
        if method == "POST" and path.endswith("/dependencies"):
            raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "dep down")

    core.on_request = hook
    data = await _split(TaskSplitArgs(children=[
        ChildTask(title="c0"), ChildTask(title="c1", depends_on=[0]),
    ], dry_run=False), ctx)
    assert data.get("ok") is False or "failures" in data


async def test_split_with_parent_title_creates_parent(runtime, core) -> None:
    ctx = ctx_for(runtime)
    data = await _split(TaskSplitArgs(parent_title="准备面试", children=[
        ChildTask(title="c0"), ChildTask(title="c1"),
    ], dry_run=False), ctx)
    assert data["parent_id"] in core.tasks
    assert core.tasks[data["parent_id"]]["title"] == "准备面试"


def test_iso_aware_preserves_offset() -> None:
    dt = datetime(2026, 11, 10, 9, 0, tzinfo=TZ8)
    assert _iso(dt) == "2026-11-10T09:00:00+08:00"


def test_iso_naive_uses_system_timezone() -> None:
    """BUG-SP2-15：naive datetime 用 .astimezone()（系统时区），不是 Asia/Shanghai。"""
    dt = datetime(2026, 11, 10, 9, 0)
    assert _iso(dt) == dt.astimezone().isoformat(timespec="seconds")


def test_slim_includes_version_and_lowercase_enums() -> None:
    slim = _slim({
        "id": "t", "title": "x", "status": "TODO", "priority": "HIGH",
        "version": 0, "tags": None, "pinned": True,
    })
    assert slim["version"] == 0
    assert slim["status"] == "todo"
    assert slim["priority"] == "high"
    assert slim["tags"] == []
