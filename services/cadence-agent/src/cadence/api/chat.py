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
from ..runtime.errors import ErrorCode, ToolError
from ..runtime.loop import TurnOutcome
from ..runtime.messages import humanize, humanize_event, redact_protocol_text
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
    base_emit = make_emitter(buf)
    # 记下本轮出现过的 error 事件：模型有时会把 501/503 消化成一句自己的话，
    # 但用户仍然该在 done 里拿到"下一步怎么办"。这是"降级不静默"的最后一环。
    seen_errors: list[dict] = []

    async def emit(event: str, data: dict) -> None:
        if event == sse.EVENT_ERROR:
            seen_errors.append(data)
        await base_emit(event, data)

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
            # L2-3：单轮总超时（默认 60s）。它比 LLM read（15s）大，所以正常情况下
            # 永远轮不到它；一旦轮到，说明"规划 + 多步工具"整体失控 —— 这时必须收尾，
            # 而不是让用户对着一个永远转圈的页面。
            async with asyncio.timeout(rt.settings.turn_timeout_s):
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
        except TimeoutError:
            trace.add(KIND_GUARD, "runtime.turn_timeout", ok=False,
                      detail={"turn_timeout_s": rt.settings.turn_timeout_s})
            timeout_err = ToolError(ErrorCode.TURN_TIMEOUT, "单轮超时")
            outcome = _timeout_outcome(ctx, trace, timeout_err, rt.settings.turn_timeout_s)
            await emit(sse.EVENT_ERROR, humanize_event(timeout_err))
            ended_by = "timeout"
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

            # L2：错误转译只发生在这一层（以及它调用的 messages.humanize），
            # 保证 SSE error 与 done 两处文案一致，且**永远不把 501/503 的原样 message
            # 透给用户**。码本身保持原样，评测按码统计不受影响。
            suggestions = _suggestions(outcome, seen_errors)
            error_code = (
                outcome.error.code.value
                if outcome is not None and outcome.error is not None
                else _last_error_code(seen_errors)
            )

            trace.close(
                cost_yuan=outcome.cost_yuan if outcome is not None else 0.0,
                total_ms=total_ms,
                degraded=bool(outcome.degraded) if outcome is not None else True,
            )
            await base_emit(
                sse.EVENT_DONE,
                {
                    "traceId": trace_id,
                    "costYuan": outcome.cost_yuan if outcome is not None else 0.0,
                    "degraded": bool(outcome.degraded) if outcome is not None else False,
                    "degradeReason": outcome.degrade_reason if outcome is not None else "",
                    "steps": outcome.steps if outcome is not None else ctx.budget.steps,
                    "endedBy": ended_by,
                    "text": redact_protocol_text(outcome.text) if outcome is not None else "",
                    "actionsTaken": outcome.actions_taken if outcome is not None else [],
                    "suggestions": suggestions,
                    # L4 报告头必须带 prompt_version，否则 v3/v4 的数据不可比。
                    # done 是离"这一轮用了哪版 prompt"最近的地方。
                    "promptVersion": trace.prompt_version,
                    "errorCode": error_code,
                },
            )
            await buf.close()


def _timeout_outcome(
    ctx: TurnContext, trace: TraceRecorder, error: ToolError, turn_timeout_s: float
) -> TurnOutcome:
    """整轮超时的收尾：保留已完成的 actions，明确说"没做完"，不假装成功。"""
    progress = "、".join(ctx.actions_taken) if ctx.actions_taken else "还没有执行任何修改"
    text = (
        f"这一轮超过 {turn_timeout_s:.0f} 秒还没做完，我把它停下来了。"
        f"已经完成的：{progress}。剩下的这次没做，说「继续」我从这里往下接。"
    )
    return TurnOutcome(
        text=text,
        actions_taken=list(ctx.actions_taken),
        error=error,
        degraded=ctx.degraded,
        degrade_reason=ctx.degrade_reason,
        steps=ctx.budget.steps,
        cost_yuan=round(ctx.budget.cost_yuan, 6),
        aborted=error.code.value,
        tier=ctx.tier,
    )


def _last_error_code(seen_errors: list[dict]) -> str:
    for data in reversed(seen_errors):
        code = str(data.get("code") or "")
        if code and code != "DEGRADED":
            return code
    return ""


def _suggestions(outcome, seen_errors: list[dict] | None = None) -> list[dict]:
    """把错误转译的建议带进 done 事件；没有错误时为空列表（不是 None）。

    优先用本轮最终错误；若模型已经把工具错误消化成正常回答（outcome 无 error），
    就把流里出现过的建议并进来 —— 用户看到的 done 仍然"可行动"。
    """
    collected: list[dict] = []
    if outcome is not None and outcome.error is not None:
        _text, collected = humanize(outcome.error)
    for data in seen_errors or []:
        for item in data.get("suggestions") or []:
            if item not in collected:
                collected.append(item)
    return collected
