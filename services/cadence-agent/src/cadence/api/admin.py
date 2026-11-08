"""GET /stream/{id} · POST /confirm · GET /trace/{id} · GET /health。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..deps import get_runtime
from ..guard import validators
from ..runtime.budget import Budget
from ..runtime.context import TurnContext
from ..runtime.errors import ErrorCode, ToolError
from ..runtime.trace import TraceRecorder, load_trace
from . import sse
from .schemas import ConfirmRequest, fail, ok

router = APIRouter()


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


@router.get("/stream/{stream_id}")
async def stream(stream_id: str, request: Request, last_event_id: str | None = Header(None)) -> Any:
    rt = get_runtime()
    buf = rt.streams.get(stream_id)
    if buf is None:
        # 文档：streamId 不存在返回 404，此时仍走统一外壳（SSE 里唯一用 JSON 的错误）
        return JSONResponse(status_code=404, content=fail(404, "stream 不存在或已过期"))

    try:
        after = int(last_event_id or 0)
    except ValueError:
        after = 0

    async def generate():
        try:
            async for event in buf.tail(after):
                if await request.is_disconnected():
                    break
                if event is None:
                    yield sse.HEARTBEAT_FRAME
                else:
                    yield event.frame()
        finally:
            # 客户端断开不取消循环 —— 这正是"循环与连接解耦"的意义
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 确认
# ---------------------------------------------------------------------------


@router.post("/confirm")
async def confirm(req: ConfirmRequest) -> JSONResponse:
    rt = get_runtime()

    if not req.approve:
        discarded = rt.confirms.discard(req.confirm_token)
        return JSONResponse(status_code=200, content=ok({"applied": 0, "ids": [], "discarded": discarded}))

    pending = rt.confirms.take(req.confirm_token)
    if pending is None:
        return JSONResponse(
            status_code=409,
            content=fail(409, "确认令牌不存在、已用过或已过期，需要重新发起操作"),
        )

    tool = rt.registry.get(pending.tool)
    if tool is None:
        return JSONResponse(status_code=404, content=fail(404, f"工具 {pending.tool} 不存在"))

    trace_id = rt.new_trace_id()
    trace = TraceRecorder(trace_id, rt.settings.resolved_trace_dir, session_id="confirm")
    ctx = TurnContext(
        settings=rt.settings,
        core=rt.core,
        trace=trace,
        confirms=rt.confirms,
        trace_id=trace_id,
        session_id="confirm",
        budget=Budget(max_steps=1, max_tokens=rt.settings.token_budget_per_turn),
    )

    started = time.perf_counter()
    try:
        # ⚠️ 这里不重复做 id 白名单校验：参数在**签发令牌那一刻**已经校验过了，
        # 令牌里存的就是那一次的参数。令牌单次有效且 5 分钟过期，语义上等价于"那一刻的快照"。
        parsed = validators.parse_args(pending.tool, tool.args_model, pending.args)
        data = await tool.handler(parsed, ctx)
    except ToolError as exc:
        trace.close(cost_yuan=0.0, total_ms=int((time.perf_counter() - started) * 1000))
        status = 409 if exc.code in (ErrorCode.PLAN_STALE, ErrorCode.CONFLICT) else 422
        return JSONResponse(status_code=status, content=fail(status, exc.message, {"code": exc.code.value}))
    except Exception as exc:  # noqa: BLE001
        trace.add("GUARD", "confirm.exception", ok=False, detail={"error": str(exc)[:300]})
        trace.close(cost_yuan=0.0, total_ms=int((time.perf_counter() - started) * 1000))
        return JSONResponse(status_code=500, content=fail(500, "执行确认操作时出错"))

    trace.close(cost_yuan=0.0, total_ms=int((time.perf_counter() - started) * 1000))

    ids: list[str] = []
    applied = 1
    if isinstance(data, dict):
        if isinstance(data.get("created"), list):
            ids = [str(t.get("id")) for t in data["created"] if isinstance(t, dict) and t.get("id")]
            applied = len(ids) or 1
        elif data.get("task_id"):
            ids = [str(data["task_id"])]
        elif isinstance(data.get("ids"), list):
            ids = [str(x) for x in data["ids"]]
            applied = len(ids)

    return JSONResponse(
        status_code=200,
        content=ok({"applied": applied, "ids": ids, "tool": pending.tool, "traceId": trace_id}),
    )


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


@router.get("/trace/{trace_id}")
async def trace_view(trace_id: str) -> JSONResponse:
    rt = get_runtime()
    data = load_trace(rt.settings.resolved_trace_dir, trace_id)
    if data is None:
        return JSONResponse(status_code=404, content=fail(404, f"trace {trace_id} 不存在"))
    return JSONResponse(status_code=200, content=ok(data))


# ---------------------------------------------------------------------------
# 健康检查（裸对象，不包外壳 —— 给运维探测）
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict[str, Any]:
    rt = get_runtime()
    try:
        return await asyncio.wait_for(rt.health(), timeout=rt.settings.core_timeout_s + 2)
    except asyncio.TimeoutError:
        return {"status": "DOWN", "core": "TIMEOUT", "llmMode": rt.mode}
