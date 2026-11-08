"""POST /chat —— 启动一次 Agent 循环并返回 streamId。

⚠️ 这里**不推流**。流在 `GET /stream/{id}`。
原因是 PRD 02 §7.6 B：循环不能绑在 HTTP 连接上，
否则连接一断循环被 cancel，写操作会停在半修改状态。
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..deps import Runtime, get_runtime
from ..runtime.budget import Budget
from ..runtime.context import TurnContext
from ..runtime.trace import KIND_GUARD, TraceRecorder
from . import sse
from .schemas import ChatRequest, ok

router = APIRouter()


@router.post("/chat", status_code=202)
async def chat(req: ChatRequest) -> JSONResponse:
    rt = get_runtime()
    trace_id = rt.new_trace_id()
    buf = rt.streams.create()

    task = asyncio.create_task(_run_turn(rt, req.session_id, req.message, trace_id, buf))
    rt.track(task)

    # 202 里的 degraded 只能是"启动时已知的降级"（例如没配推理模型）。
    # 真正的降级以 done 事件为准 —— 这个时序语义在 docs/接口文档.md 已补充说明。
    static_degraded = not rt.settings.deepseek_api_key.strip() and rt.mode == "live"

    return JSONResponse(
        status_code=202,
        content=ok(
            {
                "streamId": buf.stream_id,
                "traceId": trace_id,
                "degraded": static_degraded,
            }
        ),
    )


# ---------------------------------------------------------------------------


def make_emitter(buf: sse.StreamBuffer):
    async def emit(event: str, data: dict) -> None:
        await buf.append(event, data)

    return emit


async def _run_turn(
    rt: Runtime,
    session_id: str,
    message: str,
    trace_id: str,
    buf: sse.StreamBuffer,
) -> None:
    emit = make_emitter(buf)
    started = time.perf_counter()
    outcome = None
    ended_by = "finish"

    # 会话级串行化：同一个 session 的第二条消息排队，不会互相覆盖会话状态
    async with rt.sessions.lock(session_id):
        session = rt.sessions.get(session_id)
        trace = TraceRecorder(trace_id, rt.settings.resolved_trace_dir, session_id=session_id)
        ctx = TurnContext(
            settings=rt.settings,
            core=rt.core,
            trace=trace,
            confirms=rt.confirms,
            trace_id=trace_id,
            session_id=session_id,
            budget=Budget(
                max_steps=rt.settings.step_budget,
                max_tokens=rt.settings.token_budget_per_turn,
            ),
        )

        try:
            outcome = await rt.loop.run_turn(
                ctx,
                user_message=message,
                session=session,
                emit=emit,
            )
            if outcome.error is not None:
                ended_by = "aborted"
            elif outcome.confirm is not None:
                ended_by = "confirm"
            elif outcome.clarify is not None:
                ended_by = "clarify"
        except asyncio.CancelledError:
            trace.add(KIND_GUARD, "runtime.cancelled", ok=False, detail={})
            await emit(sse.EVENT_ERROR, {"code": "CANCELLED", "message": "本轮被取消"})
            ended_by = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - 任何未预期错误都要变成结构化事件，而不是断流
            trace.add(KIND_GUARD, "runtime.exception", ok=False, detail={"error": f"{type(exc).__name__}: {exc}"[:400]})
            await emit(
                sse.EVENT_ERROR,
                {"code": "INTERNAL_ERROR", "message": "服务内部错误，这一次没有完成。可以再试一次。"},
            )
            ended_by = "error"
        finally:
            total_ms = int((time.perf_counter() - started) * 1000)
            session.add_turn("user", message, keep=rt.settings.session_recent_turns)
            if outcome is not None and outcome.text:
                session.add_turn("assistant", outcome.text, keep=rt.settings.session_recent_turns)
            session.last_trace_id = trace_id
            session.pending_confirm = outcome.confirm if outcome is not None else None
            rt.sessions.save(session)

            trace.close(
                cost_yuan=outcome.cost_yuan if outcome is not None else 0.0,
                total_ms=total_ms,
                degraded=bool(outcome.degraded) if outcome is not None else True,
            )
            await emit(
                sse.EVENT_DONE,
                {
                    "traceId": trace_id,
                    "costYuan": outcome.cost_yuan if outcome is not None else 0.0,
                    "degraded": bool(outcome.degraded) if outcome is not None else False,
                    "degradeReason": outcome.degrade_reason if outcome is not None else "",
                    "steps": outcome.steps if outcome is not None else ctx.budget.steps,
                    "endedBy": ended_by,
                    "text": outcome.text if outcome is not None else "",
                    "actionsTaken": outcome.actions_taken if outcome is not None else [],
                },
            )
            await buf.close()
