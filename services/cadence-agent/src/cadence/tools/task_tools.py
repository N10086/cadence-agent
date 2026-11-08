"""任务类工具：search / create / update / complete / delete / split。

设计约束（PRD 05）：
- P1 返回值给模型读：列表截断、只给判断需要的字段
- P2 id 不能自由生成：schema 里写明"必须来自本轮 task.search"，guard 做白名单
- P3 写操作幂等：创建自动注入 idempotency_key
- P4 错误可行动：统一错误结构带 suggestions
- P7 描述里写禁忌，比写"能做什么"更有用
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..guard.permissions import Permission
from ..runtime.context import TurnContext
from ..runtime.errors import ErrorCode, ToolError
from .base import Tool

StatusLiteral = Literal["todo", "doing", "done", "cancelled"]
PriorityLiteral = Literal["low", "normal", "high", "urgent"]

STATUS_MAP = {"todo": "TODO", "doing": "DOING", "done": "DONE", "cancelled": "CANCELLED"}
PRIORITY_MAP = {"low": "LOW", "normal": "NORMAL", "high": "HIGH", "urgent": "URGENT"}


# ---------------------------------------------------------------------------
# 参数模型
# ---------------------------------------------------------------------------


class TaskSearchArgs(BaseModel):
    keyword: str | None = Field(None, description="标题/描述关键词，模糊匹配")
    status: StatusLiteral | None = Field(None, description="状态过滤")
    priority: PriorityLiteral | None = Field(None, description="优先级过滤")
    due_before: datetime | None = Field(None, description="截止早于此（ISO8601 带时区）")
    due_after: datetime | None = Field(None, description="截止晚于此（ISO8601 带时区）")
    tag: str | None = Field(None, description="标签精确匹配")
    limit: int = Field(10, ge=1, le=20, description="最多返回几条，默认 10")


class TaskCreateArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="任务标题")
    description: str | None = Field(None, max_length=4000, description="详细描述，可省略")
    priority: PriorityLiteral = Field("normal", description="low/normal/high/urgent")
    start_at: datetime | None = Field(None, description="开始时间（带时区）")
    due_at: datetime | None = Field(None, description="截止时间（带时区）")
    estimate_min: int | None = Field(None, ge=5, le=480, description="预估分钟数，5-480")
    parent_id: str | None = Field(None, description="父任务 id，须来自本轮 task.search")
    tags: list[str] = Field(default_factory=list, description="标签")
    rrule: str | None = Field(
        None, max_length=200, description="重复规则，如 FREQ=WEEKLY;BYDAY=MO，需同时给 start_at"
    )
    pinned: bool = Field(False, description="true 表示重排期时不移动")
    idempotency_key: str | None = Field(None, description="一般不用传，系统自动生成")


class TaskUpdateArgs(BaseModel):
    task_id: str = Field(..., description="任务 id，须来自本轮 task.search")
    version: int = Field(..., ge=0, description="乐观锁版本号，须来自本轮 task.search，不要猜")
    patch: dict[str, Any] = Field(
        ...,
        description="只放要改的字段，可用键：title/description/priority/start_at/due_at/estimate_min/pinned/rrule/tags",
    )


class TaskCompleteArgs(BaseModel):
    task_id: str = Field(..., description="任务 id，须来自本轮 task.search")
    version: int = Field(..., ge=0, description="乐观锁版本号，须来自本轮 task.search")


class TaskDeleteArgs(BaseModel):
    task_id: str = Field(..., description="任务 id，须来自本轮 task.search")
    confirm_token: str | None = Field(None, description="首次必须为空；系统返回令牌给用户确认后再回传")


class ChildTask(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="子任务标题")
    estimate_min: int | None = Field(None, ge=5, le=480, description="预估分钟数")
    priority: PriorityLiteral = "normal"
    depends_on: list[int] | None = Field(None, description="依赖的子任务下标（从 0 开始）")


class TaskSplitArgs(BaseModel):
    parent_id: str | None = Field(None, description="父任务 id，须来自本轮 task.search")
    parent_title: str | None = Field(None, description="没有父任务时就地创建一个")
    children: list[ChildTask] = Field(..., min_length=2, max_length=7, description="2-7 个子任务")
    dry_run: bool = Field(True, description="true 只预估不落库；用户确认后再传 false")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # 无时区的时间一律按本机时区解释（Core 侧是 OffsetDateTime，丢了偏移会算错"下周三"）
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def _slim(task: dict[str, Any]) -> dict[str, Any]:
    """给模型看的任务摘要。

    `version` 必须给 —— 否则模型没法调 update/complete。
    （PRD 05 §3.1 说"不给 version"，与 §3.3"必须从 search 拿到 version"矛盾；
      这里以正确性优先，已在 docs/接口文档.md 记录。）
    """
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": (task.get("status") or "").lower(),
        "priority": (task.get("priority") or "").lower(),
        "start_at": task.get("startAt"),
        "due_at": task.get("dueAt"),
        "estimate_min": task.get("estimateMin"),
        "pinned": task.get("pinned", False),
        "rrule": task.get("rrule"),
        "tags": task.get("tags") or [],
        "version": task.get("version"),
    }


_PATCH_KEYS = {
    "title": "title",
    "description": "description",
    "priority": "priority",
    "start_at": "startAt",
    "due_at": "dueAt",
    "estimate_min": "estimateMin",
    "pinned": "pinned",
    "rrule": "rrule",
    "tags": "tags",
}


def _patch_body(patch: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key, value in patch.items():
        target = _PATCH_KEYS.get(key)
        if target is None:
            continue
        if key == "priority" and isinstance(value, str):
            value = PRIORITY_MAP.get(value.lower(), value.upper())
        if key in ("start_at", "due_at") and isinstance(value, str):
            try:
                value = _iso(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                pass
        body[target] = value
    return body


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


async def _search(args: TaskSearchArgs, ctx: TurnContext) -> dict[str, Any]:
    params = {
        "keyword": args.keyword,
        "status": STATUS_MAP.get(args.status) if args.status else None,
        "priority": PRIORITY_MAP.get(args.priority) if args.priority else None,
        "dueBefore": _iso(args.due_before),
        "dueAfter": _iso(args.due_after),
        "tag": args.tag,
        "limit": args.limit,
    }
    data = await ctx.core.request(
        "GET", "/api/v1/tasks", params=params, trace_id=ctx.trace_id, tool_name="task.search"
    )
    raw_items = (data or {}).get("items") or []
    ctx.remember(raw_items)
    items = [_slim(t) for t in raw_items]
    return {
        "items": items,
        "has_more": bool((data or {}).get("hasMore")),
        "total": (data or {}).get("total", len(items)),
    }


async def _create(args: TaskCreateArgs, ctx: TurnContext) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": args.title,
        "priority": PRIORITY_MAP.get(args.priority, "NORMAL"),
        "pinned": args.pinned,
        "tags": args.tags,
    }
    for key, value in (
        ("description", args.description),
        ("startAt", _iso(args.start_at)),
        ("dueAt", _iso(args.due_at)),
        ("estimateMin", args.estimate_min),
        ("parentId", args.parent_id),
        ("rrule", args.rrule),
    ):
        if value is not None:
            body[key] = value

    data = await ctx.core.request(
        "POST",
        "/api/v1/tasks",
        body=body,
        trace_id=ctx.trace_id,
        idempotency_key=args.idempotency_key or ctx.idempotency_key("create"),
        tool_name="task.create",
    )
    ctx.remember(data)
    ctx.note_action(f"创建任务「{args.title}」")
    slim = _slim(data or {})
    return {"task": slim}


async def _update(args: TaskUpdateArgs, ctx: TurnContext) -> dict[str, Any]:
    body = _patch_body(args.patch)
    if not body:
        raise ToolError(
            ErrorCode.VALIDATION_ERROR,
            "patch 里没有可识别的字段，什么都不会改",
            details={"received": args.patch, "allowed": sorted(_PATCH_KEYS)},
        )
    body["version"] = args.version

    data = await ctx.core.request(
        "PATCH",
        f"/api/v1/tasks/{args.task_id}",
        body=body,
        trace_id=ctx.trace_id,
        tool_name="task.update",
    )
    task = (data or {}).get("task") or {}
    ctx.remember(task)
    ctx.note_action(f"修改任务「{task.get('title', args.task_id)}」")
    warnings = (data or {}).get("warnings") or []
    return {
        "task": _slim(task),
        "warnings": warnings,  # 冲突只是提示，不是错误（FR-8）
    }


async def _complete(args: TaskCompleteArgs, ctx: TurnContext) -> dict[str, Any]:
    data = await ctx.core.request(
        "POST",
        f"/api/v1/tasks/{args.task_id}:complete",
        body={"version": args.version},
        trace_id=ctx.trace_id,
        tool_name="task.complete",
    )
    ctx.remember(data)
    ctx.note_action(f"完成任务「{(data or {}).get('title', args.task_id)}」")
    return {"task": _slim(data or {})}


async def _delete(args: TaskDeleteArgs, ctx: TurnContext) -> dict[str, Any]:
    """两阶段删除。

    这里的 `confirm_token` 是 **Agent 的 cf_** 令牌（用户确认过方案），
    Core 自己的 `ct_` 令牌由本函数在内层自动处理 —— 因为人已经确认过一次了，
    没必要让用户为同一件事点两次。
    """
    path = f"/api/v1/tasks/{args.task_id}"
    try:
        await ctx.core.request("DELETE", path, trace_id=ctx.trace_id, tool_name="task.delete")
    except ToolError as exc:
        if exc.code is not ErrorCode.CONFIRM_REQUIRED:
            raise
        core_token = (exc.details or {}).get("confirmToken")
        if not core_token:
            raise
        await ctx.core.request(
            "DELETE",
            path,
            trace_id=ctx.trace_id,
            confirm_token=core_token,
            tool_name="task.delete",
        )
    ctx.note_action(f"删除任务 {args.task_id}")
    return {"deleted": True, "task_id": args.task_id}


async def _split(args: TaskSplitArgs, ctx: TurnContext) -> dict[str, Any]:
    """拆解成子任务。

    ⚠️ 契约缺口：Core 的 `POST /tasks:batch` 只支持**更新**（每条带 version），
    不支持批量创建，所以这里用 N 次幂等创建。
    失败时**不回滚**，报告「已建 k/n」—— 对应 ADR D9「不回滚，但报告进度」。
    """
    parent_id = args.parent_id
    plan = [
        {
            "title": c.title,
            "priority": PRIORITY_MAP.get(c.priority, "NORMAL"),
            "estimate_min": c.estimate_min,
            "depends_on": c.depends_on or [],
        }
        for c in args.children
    ]

    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "parent_id": parent_id,
            "parent_title": args.parent_title,
            "will_create": plan,
            "count": len(plan),
            "note": "这是预估。确认后请用 dry_run=false 再调一次。",
        }

    created: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    if parent_id is None and args.parent_title:
        parent = await ctx.core.request(
            "POST",
            "/api/v1/tasks",
            body={"title": args.parent_title, "priority": "NORMAL"},
            trace_id=ctx.trace_id,
            idempotency_key=ctx.idempotency_key("split:parent"),
            tool_name="task.split",
        )
        ctx.remember(parent)
        parent_id = (parent or {}).get("id")

    for index, child in enumerate(args.children):
        try:
            data = await ctx.core.request(
                "POST",
                "/api/v1/tasks",
                body={
                    "title": child.title,
                    "priority": PRIORITY_MAP.get(child.priority, "NORMAL"),
                    "parentId": parent_id,
                    **({"estimateMin": child.estimate_min} if child.estimate_min else {}),
                },
                trace_id=ctx.trace_id,
                idempotency_key=ctx.idempotency_key(f"split:{index}"),
                tool_name="task.split",
            )
            ctx.remember(data)
            created.append(_slim(data or {}))
        except ToolError as exc:
            failures.append({"index": index, "title": child.title, "code": exc.code.value, "message": exc.message})

    # 子任务之间的依赖：全部建完再连，避免引用不存在的 id
    for index, child in enumerate(args.children):
        for dep_index in child.depends_on or []:
            if 0 <= dep_index < len(created) and index < len(created) and dep_index != index:
                try:
                    await ctx.core.request(
                        "POST",
                        f"/api/v1/tasks/{created[index]['id']}/dependencies",
                        body={"dependsOnId": created[dep_index]["id"]},
                        trace_id=ctx.trace_id,
                        tool_name="task.split",
                    )
                except ToolError:
                    pass  # 依赖建不上不影响子任务本身已创建的事实

    ctx.note_action(f"拆解出 {len(created)} 个子任务")
    payload: dict[str, Any] = {
        "ok": not failures,
        "parent_id": parent_id,
        "created": created,
        "count": len(created),
    }
    if failures:
        payload["partial"] = True
        payload["failures"] = failures
        payload["note"] = f"已创建 {len(created)}/{len(args.children)} 个，未回滚已成功的部分。"
    return payload


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="task.search",
        description=(
            "查询任务。不带任何过滤条件时等于「列出全部」。"
            "**这是获取 task_id 和 version 的唯一途径** —— 任何写操作之前，"
            "都必须先用这个工具拿到真实的 id 和 version。"
            "返回最多 20 条，被截断时 has_more 为 true。"
        ),
        args_model=TaskSearchArgs,
        handler=_search,
        permission=Permission.READ,
    ),
    Tool(
        name="task.create",
        description=(
            "创建一个任务。一次只建一个 —— 需要多个时请调用多次，"
            "这样你能看到每一步的结果。建子任务用 parent_id。"
            "**禁忌**：不要在同一次调用里试图创建多个任务。"
        ),
        args_model=TaskCreateArgs,
        handler=_create,
        permission=Permission.WRITE,
    ),
    Tool(
        name="task.update",
        description=(
            "局部更新一个任务，只传要改的字段。"
            "**必须提供 version**，它来自本轮 task.search 的返回；"
            "version 不对会被拒绝（409），这时要重新 search 而不是猜一个更大的数字。"
            "**禁忌**：不要用它改 status，状态变化请用 task.complete。"
        ),
        args_model=TaskUpdateArgs,
        handler=_update,
        permission=Permission.WRITE,
    ),
    Tool(
        name="task.complete",
        description=(
            "把一个任务标记为已完成。需要 task_id 和 version。"
            "幂等：已经完成的任务再调不会报错。"
            "如果该任务有未完成的前置依赖，会被拒绝，这时要先完成前置任务。"
        ),
        args_model=TaskCompleteArgs,
        handler=_complete,
        permission=Permission.WRITE,
    ),
    Tool(
        name="task.delete",
        description=(
            "删除一个任务（软删除，可恢复）。"
            "**禁止**在未确认的情况下调用。**禁止**批量删除。"
            "若用户说「清理一下」，必须先 task.search 列出候选，再逐个确认。"
            "首次调用不要传 confirm_token，系统会把令牌返回给用户确认。"
        ),
        args_model=TaskDeleteArgs,
        handler=_delete,
        permission=Permission.DESTRUCTIVE,
    ),
    Tool(
        name="task.split",
        description=(
            "把一个任务拆成 2-7 个子任务。超过 7 条说明分解粒度太粗，应该重新分解。"
            "默认 dry_run=true 只返回预估，用户确认后才用 dry_run=false 真正创建。"
            "**禁忌**：不要用它排期或改已有任务的时间。"
        ),
        args_model=TaskSplitArgs,
        handler=_split,
        permission=Permission.WRITE,
        supports_dry_run=True,
    ),
]
