"""POST /chat —— 启动一次 Agent 循环并返回 streamId。

⚠️ 这里**不推流**。流在 `GET /stream/{id}`。
原因是 PRD 02 §7.6 B：循环不能绑在 HTTP 连接上，
否则连接一断循环被 cancel，写操作会停在半修改状态。

**L3 并发入口（三层，从外到内）**

    准入队列（64，满 → 503 OVERLOADED，不排队）
        │
        ▼
    会话锁（每 session 一把，等 20s → 409 SESSION_BUSY）
        │
        ▼
    LLM 信号量（16，叶子；只在 provider 调用点持有）

排队时间从 `queue.enter` 到 `queue.started` 之间计量，供 L4 的 `T_queue` 用。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..deps import Runtime, get_runtime
from ..llm.router import route
from ..promptlib import system_prompt
from ..runtime.budget import Budget
from ..runtime.context import TurnContext
from ..runtime.errors import ErrorCode, ToolError
from ..runtime.loop import TurnOutcome
from ..runtime.messages import humanize, humanize_event, redact_protocol_text
from ..runtime.queue import Overloaded
from ..runtime.trace import KIND_GUARD, TraceRecorder
from . import sse
from .schemas import ChatRequest, ok

router = APIRouter()


@dataclass
class TurnHandle:
    """一次已受理的 turn。压测脚本直接用它，不走 HTTP。"""

    stream_id: str
    trace_id: str
    task: asyncio.Task


async def start_turn(rt: Runtime, session_id: str, message: str) -> TurnHandle:
    """受理一个 turn：先过准入队列，再建流 + 起任务 + 挂看门狗。

    准入失败抛 `ToolError(OVERLOADED)`，由 FastAPI 的异常处理器落成 503；
    **绝不**在这里等队列空位 —— 排队到死比 503 更糟。
    """
    trace_id = rt.new_trace_id()
    try:
        rt.queue.enter(trace_id)
    except Overloaded as exc:
        raise ToolError(
            ErrorCode.OVERLOADED,
            str(exc),
            suggestions=[{"hint": "几秒后重试", "reason": "入口队列已满，系统主动拒绝而不是排队"}],
            details={"capacity": rt.queue.capacity},
        ) from exc

    buf = rt.streams.create()
    try:
        task = asyncio.create_task(
            _run_turn(rt, session_id, message, trace_id, buf),
            name=f"turn:{trace_id}",
        )
    except BaseException:  # 极端情况（任务创建失败）也要把名额还回去
        rt.queue.leave(trace_id)
        await buf.close()
        raise

    rt.track(task)
    watcher = rt.watch(task, trace_id)
    task.add_done_callback(lambda _t: watcher.cancel())
    return TurnHandle(stream_id=buf.stream_id, trace_id=trace_id, task=task)


@router.post("/chat", status_code=202)
async def chat(req: ChatRequest) -> JSONResponse:
    rt = get_runtime()
    handle = await start_turn(rt, req.session_id, req.message)

    # 202 里的 degraded 只能是"启动时已知的降级"（例如没配推理模型）。
    # 真正的降级以 done 事件为准 —— 这个时序语义在 docs/接口文档.md 已补充说明。
    static_degraded = not rt.settings.deepseek_api_key.strip() and rt.mode == "live"

    return JSONResponse(
        status_code=202,
        content=ok(
            {
                "streamId": handle.stream_id,
                "traceId": handle.trace_id,
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
    """准入名额的唯一归还点：无论正常、超时、取消、异常，都必须 leave。

    `started` 必须在**这里**取：等会话锁的时间也是用户真实等待的一部分。
    如果放到拿到锁之后再计时，SESSION_BUSY 的 trace 会写成 `total_ms=0`，
    而用户实际已经等了 20s —— 评测的 latency 口径直接失真。
    """
    started = time.perf_counter()
    rt.queue.started(trace_id)
    try:
        await _run_turn_serialized(rt, session_id, message, trace_id, buf, started)
    finally:
        rt.queue.leave(trace_id)


async def _run_turn_serialized(
    rt: Runtime,
    session_id: str,
    message: str,
    trace_id: str,
    buf: sse.StreamBuffer,
    started: float,
) -> None:
    base_emit = make_emitter(buf)
    # 记下本轮出现过的 error 事件：模型有时会把 501/503 消化成一句自己的话，
    # 但用户仍然该在 done 里拿到"下一步怎么办"。这是"降级不静默"的最后一环。
    seen_errors: list[dict] = []

    async def emit(event: str, data: dict) -> None:
        if event == sse.EVENT_ERROR:
            seen_errors.append(data)
        await base_emit(event, data)

    outcome = None
    ended_by = "finish"

    # L3-3：会话级串行化，但**有上限**。无上限的排队等于把用户挂死；
    # 等满 20s 就 409 SESSION_BUSY，让客户端换会话或重试。
    try:
        lock = await rt.sessions.acquire(session_id, timeout_s=rt.settings.session_lock_timeout_s)
    except ToolError as busy:
        await _finish_busy(rt, session_id, trace_id, buf, busy, base_emit, started)
        return

    try:
        session = rt.sessions.get(session_id)
        # prompt_version 在建立 trace 时就要知道，否则收尾得把整个文件读出来重写 META 行。
        # 路由是纯函数（同一输入永远同一结果），这里先算一次，Loop 里还会用同样的结果。
        tier = route(message).tier.value
        trace = TraceRecorder(
            trace_id,
            rt.settings.resolved_trace_dir,
            session_id=session_id,
            prompt_version=system_prompt(tier)[1],
        )
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
            #
            # L3-4：watchdog 用的是同一个 turn_timeout_s，但**只打栈不取消**，
            # 取消仍然只在这里发生 —— 不能有两套取消器。
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
                    "costYuan": round(outcome.cost_yuan, 6) if outcome is not None else 0.0,
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
    finally:
        # 唯一释放点：取消/超时/异常路径都必须走到这里，否则压测会看到 lock_leak>0
        lock.release()


async def _finish_busy(
    rt: Runtime,
    session_id: str,
    trace_id: str,
    buf: sse.StreamBuffer,
    error: ToolError,
    emit,
    started: float,
) -> None:
    """等会话锁超时：写 trace + 推 error + 用 done 收尾（endedBy=busy）。

    `total_ms` 必须是从 `_run_turn` 入口算起的真实等待，而不是 0：
    用户为这一轮等了 20s，评测却记成 0ms，会让"会话忙"在时延统计里凭空消失。
    """
    total_ms = int((time.perf_counter() - started) * 1000)
    trace = TraceRecorder(trace_id, rt.settings.resolved_trace_dir, session_id=session_id)
    trace.add(
        KIND_GUARD, "runtime.session_busy", ok=False,
        detail={"session": session_id, "timeout_s": rt.settings.session_lock_timeout_s},
    )
    await emit(sse.EVENT_ERROR, humanize_event(error))
    await emit(
        sse.EVENT_DONE,
        {
            "traceId": trace_id,
            "costYuan": 0.0,
            "degraded": False,
            "degradeReason": "",
            "steps": 0,
            "endedBy": "busy",
            "text": humanize(error)[0],
            "actionsTaken": [],
            "suggestions": humanize(error)[1],
            "promptVersion": trace.prompt_version,
            "errorCode": error.code.value,
        },
    )
    trace.close(cost_yuan=0.0, total_ms=total_ms, degraded=False)
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
