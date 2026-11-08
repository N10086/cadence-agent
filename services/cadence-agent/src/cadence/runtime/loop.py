"""★ 多步循环状态机。

    START ──► CLASSIFY ──[歧义]──► CLARIFY ──► 结束本轮（等用户回答）
                │
                ▼
              PLAN ──► ACT ──► OBSERVE ──[还需动作?]──┐
                        ▲                            │是
                        └────────────────────────────┘
                                         │否
                                         ▼
                                      FINISH

每个状态都有守卫（`docs/loop.md` 有完整表）：

| 状态 | 守卫 |
|---|---|
| CLASSIFY | 歧义 → 必须先问，不许猜 |
| ACT | 参数校验 + id 白名单 + 权限 + 确认令牌 + 指纹去重 |
| OBSERVE | 步数/token/成本预算 |
| FINISH | `meta.finish` 的 `actions_taken` 必须真实 |

**两处"停下来"是本质差别**：CLARIFY（歧义）和 CONFIRM（破坏性操作）。
教程版系统不会停，所以它敢乱猜、敢乱删。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from ..brain import intent as brain_intent
from ..config import Settings
from ..guard import injection, permissions, validators
from ..llm.base import LLMClient, LLMError, LLMResponse
from ..llm.cost import cost_of
from ..llm.retry import Breaker, call_with_resilience
from ..llm.router import route
from ..promptlib import system_prompt
from ..runtime.context import TurnContext
from ..runtime.errors import BudgetExceeded, ErrorCode, ToolError
from ..runtime.fingerprint import FingerprintTracker
from ..runtime.session import SessionState, compress_messages, estimate_messages
from ..tools.base import Tool, ToolResult
from ..tools.registry import ToolRegistry
from . import events

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class TurnOutcome:
    text: str = ""
    actions_taken: list[str] = field(default_factory=list)
    clarify: dict[str, Any] | None = None
    confirm: dict[str, Any] | None = None
    error: ToolError | None = None
    degraded: bool = False
    degrade_reason: str = ""
    steps: int = 0
    cost_yuan: float = 0.0
    aborted: str = ""
    tier: str = "fast"

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------


class Loop:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: ToolRegistry,
        llm: LLMClient,
        breaker: Breaker | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.llm = llm
        self.breaker = breaker
        # 时间基准必须注入（PRD 02 §3.3）：不注入，"今天/下周三"在测试里就没法固定
        self.clock = clock or (lambda: datetime.now().astimezone())

    # ------------------------------------------------------------------

    async def run_turn(
        self,
        ctx: TurnContext,
        *,
        user_message: str,
        session: SessionState,
        emit: Emit,
        tier: str | None = None,
        llm: LLMClient | None = None,
    ) -> TurnOutcome:
        client = llm or self.llm
        started = time.perf_counter()

        # ---- CLASSIFY（路由 + 意图 + 歧义）------------------------------
        decision = route(user_message)
        chosen_tier = tier or decision.tier.value
        ctx.tier = chosen_tier
        ctx.trace.add(
            "LLM",
            "route.classify",
            detail={"tier": chosen_tier, "rule": decision.rule, "reason": decision.reason},
        )

        classify = brain_intent.classify(user_message)
        ctx.trace.add(
            "GUARD",
            "guard.classify",
            detail={"intent": classify.intent.value, "ambiguous": classify.ambiguous, "note": classify.note},
        )

        hits = injection.scan(user_message)
        if hits:
            ctx.risk_flags.extend(hits)
            ctx.trace.add("GUARD", "guard.injection_scan", ok=True, detail={"flags": hits})

        messages = self._build_messages(user_message, session, chosen_tier, ctx)

        if classify.ambiguous and classify.question:
            payload = {
                "control": "clarify",
                "question": classify.question,
                "options": [],
                "missing": classify.missing,
            }
            await emit(events.EVENT_CLARIFY, payload)
            ctx.trace.add("GUARD", "guard.clarify", detail=payload)
            session.pending_confirm = None
            return self._finish_outcome(ctx, started, clarify=payload)

        # ---- ACT / OBSERVE 循环 ----------------------------------------
        fingerprints = FingerprintTracker()
        outcome = TurnOutcome(tier=chosen_tier)
        finish_message = ""

        while True:
            if not ctx.budget.can_start_step():
                ctx.trace.add(
                    "DEGRADE",
                    "budget.step_limit",
                    ok=False,
                    detail={"max_steps": ctx.budget.max_steps, "history": ctx.budget.history},
                )
                return self._abort(ctx, started, outcome, ErrorCode.BUDGET_EXCEEDED,
                                   f"步数预算用尽（上限 {ctx.budget.max_steps} 步）")

            try:
                ctx.step_index = ctx.budget.start_step()
            except BudgetExceeded as exc:
                return self._abort(ctx, started, outcome, exc.code, exc.message)

            # 上下文超预算 → 压缩（F8）
            if estimate_messages(messages) > self.settings.max_context_tokens:
                before = estimate_messages(messages)
                messages = compress_messages(messages)
                ctx.trace.add(
                    "GUARD",
                    "guard.compress",
                    detail={"tokens_before": before, "tokens_after": estimate_messages(messages)},
                )

            llm_started = time.perf_counter()
            try:
                response = await self._call_llm(client, messages, ctx, emit, chosen_tier)
            except BudgetExceeded as exc:
                return self._abort(ctx, started, outcome, exc.code, exc.message)
            llm_ms = int((time.perf_counter() - llm_started) * 1000)
            if response is None:
                return self._abort(ctx, started, outcome, ErrorCode.UPSTREAM_UNAVAILABLE, "模型调用失败")

            # provider 回来的函数名是下划线版（task_search），这里换回逻辑名（task.search）
            for call in response.tool_calls:
                call.name = self.registry.logical_name(call.name)

            ctx.trace.add(
                "LLM",
                f"llm.step{ctx.step_index}",
                duration_ms=llm_ms,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                detail={
                    "tier": chosen_tier,
                    "model": response.model,
                    "tool_calls": [c.name for c in response.tool_calls],
                    "text_len": len(response.text or ""),
                    "reasoning": (response.reasoning or "")[:400] or None,
                },
            )

            # 无工具调用 = 模型直接回答，本轮结束
            if not response.tool_calls:
                finish_message = response.text or ""
                if not self.settings.sse_live_tokens:
                    await self._emit_text(emit, finish_message)
                outcome.text = finish_message
                return self._finish_outcome(ctx, started, text=outcome.text)

            messages.append(_assistant_message(response))

            for call in response.tool_calls:
                ctx.tool_calls += 1

                # ---- 指纹去重（F5）----
                stuck, fp = fingerprints.is_stuck(call.name, call.arguments)
                if stuck:
                    ctx.trace.add(
                        "GUARD", "guard.fingerprint", ok=False,
                        detail={"fingerprint": fp, "tool": call.name},
                    )
                    return self._abort(
                        ctx, started, outcome, ErrorCode.LOOP_DETECTED,
                        f"检测到重复调用 {call.name}（同一参数连续两次），中断并汇报进度",
                    )

                tool = self.registry.get(call.name)
                # 先报"要调用了"，再判断拦不拦 —— 前端时间线上要能看到"尝试过但被拦下"
                await emit(
                    events.EVENT_TOOL_CALL,
                    {"name": call.name, "args": call.arguments, "step": ctx.step_index},
                )

                if tool is None:
                    err = ToolError(
                        ErrorCode.VALIDATION_ERROR,
                        f"没有名为 {call.name} 的工具。可用工具：{', '.join(self.registry.names())}",
                    )
                    await self._append_tool_message(messages, call.id, err.to_tool_json(), ctx, call.name, err, 0)
                    await self._emit_failure(emit, call.name, err)
                    continue

                # ---- 权限 ----
                try:
                    permissions.assert_executable(call.name)
                except permissions.PermissionDenied as exc:
                    err = ToolError(ErrorCode.NOT_IMPLEMENTED, str(exc))
                    await self._append_tool_message(messages, call.id, err.to_tool_json(), ctx, call.name, err, 0)
                    await self._emit_failure(emit, call.name, err)
                    continue

                # ---- 参数校验（F2：缺失 → 当作歧义）----
                try:
                    parsed = validators.parse_args(call.name, tool.args_model, call.arguments)
                except ToolError as exc:
                    ctx.trace.add("GUARD", "guard.args", ok=False, detail={"tool": call.name, "error": exc.message})
                    payload = {
                        "control": "clarify",
                        "question": f"要完成这个操作还缺信息：{exc.message}。你能补充一下吗？",
                        "options": [],
                        "missing": (exc.details or {}).get("missing", []),
                    }
                    await emit(events.EVENT_CLARIFY, payload)
                    session.pending_confirm = None
                    return self._finish_outcome(ctx, started, clarify=payload)

                # ---- id 白名单（F6）----
                try:
                    validators.assert_ids_visible(call.arguments, ctx.visible_task_ids, tool_name=call.name)
                except ToolError as exc:
                    ctx.trace.add(
                        "GUARD", "guard.whitelist", ok=False,
                        detail={"tool": call.name, "offending": (exc.details or {}).get("offending_id")},
                    )
                    await self._append_tool_message(messages, call.id, exc.to_tool_json(), ctx, call.name, exc, 0)
                    await self._emit_failure(emit, call.name, exc)
                    continue

                # ---- 危险操作确认（F7）----
                affected = _affected_count(call.name, parsed)
                if permissions.needs_confirm(
                    call.name, affected=affected, batch_threshold=self.settings.destructive_batch_threshold
                ):
                    try:
                        ok, pending_payload = await self._handle_confirm(call, tool, ctx, emit, affected)
                    except ToolError as exc:
                        # 令牌过期/版本变化/作用域不符：不执行目标工具，把 PLAN_STALE 作为本轮结果。
                        return self._abort(ctx, started, outcome, exc.code, exc.message)
                    if not ok:
                        session.pending_confirm = pending_payload
                        return self._finish_outcome(ctx, started, confirm=pending_payload)

                # ---- 执行 ----
                result = await self._execute(tool, parsed, ctx)

                if isinstance(result.data, dict) and result.data.get("control") == "finish":
                    finish_message = result.data.get("message") or ""
                    unverified = result.data.get("unverified") or []
                    if unverified:
                        ctx.trace.add("GUARD", "guard.actions_unverified", ok=False, detail={"claims": unverified})
                        finish_message = (
                            finish_message
                            + "\n\n（注意：其中有些动作我其实没有执行成功，请以任务列表实际状态为准。）"
                        )
                    await self._emit_text(emit, finish_message)
                    outcome.text = finish_message
                    outcome.actions_taken = list(ctx.actions_taken)
                    return self._finish_outcome(ctx, started, text=finish_message,
                                                actions_taken=list(ctx.actions_taken))

                if isinstance(result.data, dict) and result.data.get("control") == "clarify":
                    payload = {
                        "control": "clarify",
                        "question": result.data.get("question"),
                        "options": result.data.get("options") or [],
                        "missing": result.data.get("missing") or [],
                    }
                    await emit(events.EVENT_CLARIFY, payload)
                    session.pending_confirm = None
                    return self._finish_outcome(ctx, started, clarify=payload)

                model_json = result.to_model_json()
                await self._append_tool_message(
                    messages, call.id, model_json, ctx, call.name, result.error, result.duration_ms
                )
                await emit(
                    events.EVENT_TOOL_RESULT,
                    {
                        "name": call.name,
                        "ok": result.ok,
                        "summary": result.summary,
                        "durationMs": result.duration_ms,
                    },
                )

            # ---- OBSERVE：预算复核 ----
            if ctx.budget.tokens_total > ctx.budget.max_tokens:
                return self._abort(ctx, started, outcome, ErrorCode.BUDGET_EXCEEDED, "token 预算用尽")

    # ------------------------------------------------------------------
    # LLM 调用：重试 + 熔断 + 降级
    # ------------------------------------------------------------------

    async def _call_llm(
        self,
        client: LLMClient,
        messages: list[dict[str, Any]],
        ctx: TurnContext,
        emit: Emit,
        tier: str,
    ) -> LLMResponse | None:
        async def once() -> LLMResponse:
            async for ev in client.stream(messages, self.registry.openai_tools()):
                if ev.kind == "token":
                    # 默认缓冲模式：不在这里推，等这轮确认成功由调用方一次性推，
                    # 避免"流到一半失败 → 重试 → 文本重复"。
                    # 打开 SSE_LIVE_TOKENS 时才边生成边推（体验好，但可能重复一段）。
                    if self.settings.sse_live_tokens:
                        await emit(events.EVENT_TOKEN, {"text": ev.text})
                elif ev.kind == "done" and ev.response is not None:
                    _charge(ctx, ev.response, self.settings)
                    return ev.response
            raise LLMError("流式响应没有 done 事件")

        try:
            return await call_with_resilience(
                once,
                breaker=self.breaker,
                times=self.settings.llm_retry_times,
                base_delay=self.settings.llm_retry_backoff_s,
            )
        except LLMError as exc:
            mapped = _map_llm_error(exc)

            # 失败矩阵 F1：模型输出非法 JSON。
            # PRD 的策略是"重试 1 次 → 仍失败则降级为纯文本回复"，
            # 所以这里不报错，而是给一句人话让用户重说 —— 比弹一个错误码有用。
            from ..llm.base import LLMBadOutput

            if isinstance(exc, LLMBadOutput):
                fallback = "我刚才没太理解，能不能换个说法再说一次？"
                ctx.trace.add("DEGRADE", "llm.bad_output", ok=False, detail={"error": str(exc)[:200]})
                return LLMResponse(text=fallback, model=client.name)

            if tier == "reason" and not ctx.degraded:
                # L1 降级：换 fast 策略重做本轮（单 provider 时等于换短提示）
                ctx.mark_degraded(f"reason_model_unavailable:{type(exc).__name__}")
                ctx.trace.add(
                    "DEGRADE", "llm.degrade_l1", ok=False,
                    detail={"from": tier, "to": "fast", "error": str(exc)[:200]},
                )
                await emit(
                    events.EVENT_ERROR,
                    {
                        "code": "DEGRADED",
                        "message": "推理模型不可用，已切换到快速模式，任务分解能力受限",
                        "degraded": True,
                    },
                )
                fast_messages = list(messages)
                if fast_messages and fast_messages[0].get("role") == "system":
                    fast_system, _version = system_prompt("fast")
                    fast_messages[0] = {"role": "system", "content": fast_system}
                try:
                    async def once_fast() -> LLMResponse:
                        buffer: list[str] = []
                        async for ev in client.stream(fast_messages, self.registry.openai_tools()):
                            if ev.kind == "token":
                                if self.settings.sse_live_tokens:
                                    await emit(events.EVENT_TOKEN, {"text": ev.text})
                            elif ev.kind == "done" and ev.response is not None:
                                resp = ev.response
                                _charge(ctx, resp, self.settings)
                                if not self.settings.sse_live_tokens and not resp.tool_calls:
                                    await self._emit_text(emit, resp.text or "")
                                return resp
                        raise LLMError("流式响应没有 done 事件")

                    return await call_with_resilience(once_fast, breaker=None, times=0)
                except LLMError as second:
                    mapped = _map_llm_error(second)
                    ctx.trace.add("DEGRADE", "llm.degrade_l2_failed", ok=False,
                                  detail={"error": str(second)[:200]})
            ctx.trace.add("LLM", "llm.failed", ok=False, detail={"code": mapped.code.value, "error": str(exc)[:300]})
            await emit(events.EVENT_ERROR, {"code": mapped.code.value, "message": mapped.message, "degraded": ctx.degraded})
            return None

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def _execute(self, tool: Tool, parsed: Any, ctx: TurnContext) -> ToolResult:
        started = time.perf_counter()
        try:
            data = await tool.handler(parsed, ctx)
        except ToolError as exc:
            duration = int((time.perf_counter() - started) * 1000)
            ctx.trace.add(
                "TOOL", tool.name, duration_ms=duration, ok=False,
                detail={"code": exc.code.value, "error": exc.message},
            )
            return ToolResult(ok=False, error=exc, summary=exc.message, duration_ms=duration,
                              model_payload=exc.to_tool_json())
        except Exception as exc:  # noqa: BLE001 - 任何未预期异常都要变成结构化错误
            duration = int((time.perf_counter() - started) * 1000)
            wrapped = ToolError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"工具 {tool.name} 执行失败：{type(exc).__name__}: {exc}",
            )
            ctx.trace.add("TOOL", tool.name, duration_ms=duration, ok=False,
                          detail={"code": wrapped.code.value, "error": str(exc)[:300]})
            return ToolResult(ok=False, error=wrapped, summary=wrapped.message, duration_ms=duration,
                              model_payload=wrapped.to_tool_json())

        duration = int((time.perf_counter() - started) * 1000)
        summary = _summarize(tool.name, data)
        ctx.trace.add("TOOL", tool.name, duration_ms=duration, ok=True, detail={"summary": summary})
        return ToolResult(
            ok=True,
            data=data,
            summary=summary,
            duration_ms=duration,
            model_payload=_truncate(data, self.settings),
            mutated=tool.permission in (permissions.Permission.WRITE, permissions.Permission.DESTRUCTIVE),
        )

    async def _emit_failure(self, emit: Emit, tool_name: str, err: ToolError) -> None:
        """被护栏拦下的调用也要给前端一个 tool_result，否则时间线上会缺一格。"""
        await emit(
            events.EVENT_TOOL_RESULT,
            {"name": tool_name, "ok": False, "summary": err.message, "durationMs": 0, "code": err.code.value},
        )

    async def _append_tool_message(
        self,
        messages: list[dict[str, Any]],
        call_id: str,
        payload: Any,
        ctx: TurnContext,
        tool_name: str,
        error: ToolError | None,
        duration_ms: int,
    ) -> None:
        content = injection.wrap_tool_result(
            tool_name,
            payload,
            max_chars=self.settings.tool_result_max_chars,
        )
        messages.append({"role": "tool", "tool_call_id": call_id or tool_name, "content": content})

    # ------------------------------------------------------------------
    # 确认流程
    # ------------------------------------------------------------------

    async def _handle_confirm(
        self,
        call: Any,
        tool: Tool,
        ctx: TurnContext,
        emit: Emit,
        affected: int,
    ) -> tuple[bool, dict[str, Any]]:
        """返回 (是否已确认可执行, 需要用户确认时的 payload)。"""
        token = call.arguments.get("confirm_token") if isinstance(call.arguments, dict) else None

        if token:
            pending = ctx.confirms.peek(str(token))
            if pending is not None:
                # 先校验「用户确认的方案」和「模型现在要执行的调用」是不是同一件事。
                # 不通过时抛 PLAN_STALE，绝不先 take（否则合法令牌会被白白烧掉）。
                mismatch = pending.scope_mismatch(call.name, call.arguments)
                if mismatch is not None:
                    ctx.trace.add(
                        "GUARD",
                        "guard.confirm_stale",
                        ok=False,
                        detail={"tool": call.name, "token": str(token)[:12], "reason": mismatch},
                    )
                    raise ToolError(ErrorCode.PLAN_STALE, mismatch)

                await self._assert_confirm_versions(pending, ctx, call.name)
                consumed = ctx.confirms.take(str(token))
                if consumed is not None:
                    ctx.trace.add(
                        "GUARD", "guard.confirm_ok",
                        detail={"tool": call.name, "token": str(token)[:12]},
                    )
                    return True, {}

            ctx.trace.add("GUARD", "guard.confirm_invalid", ok=False,
                          detail={"tool": call.name, "token": str(token)[:12]})
            # 令牌无效/刚好过期 → 需要重新确认

        preview = await self._build_preview(call.name, call.arguments, ctx)
        versions = {
            task_id: ctx.visible_versions[task_id]
            for task_id in _referenced_task_ids(call.arguments)
            if task_id in ctx.visible_versions
        }
        pending = ctx.confirms.issue(
            tool=call.name,
            args=dict(call.arguments),
            preview=preview,
            versions=versions,
        )
        payload = {
            "confirmToken": pending.token,
            "tool": call.name,
            "expiresInSeconds": pending.ttl_s,
            "plan": preview,
            "affected": affected,
        }
        await emit(events.EVENT_CONFIRM, payload)
        ctx.trace.add("GUARD", "guard.confirm_required", detail={"tool": call.name, "affected": affected})
        return False, payload

    async def _assert_confirm_versions(self, pending: Any, ctx: TurnContext, tool_name: str) -> None:
        """确认前重新读一次任务版本，避免「预览后、确认前」被别的请求改掉（PLAN_STALE）。

        `pending.versions` 只包含本次调用真正引用到的任务（见 `_referenced_task_ids`），
        因此这里每个任务一次 GET 就够，不会因为无关任务变化把确认卡死。
        """
        if not pending.versions:
            # TODO(L0-5): split 只给 children 时没有可校验的旧版本，先跳过；
            # 新建子任务本身没有「旧版本被改」的语义，真正的并发保护仍由 Core 兜底。
            ctx.trace.add(
                "GUARD", "guard.confirm_versions_skipped",
                detail={"tool": tool_name, "reason": "no versions in token"},
            )
            return

        for task_id, expected in pending.versions.items():
            try:
                data = await ctx.core.request(
                    "GET", f"/api/v1/tasks/{task_id}", trace_id=ctx.trace_id, tool_name=tool_name
                )
            except ToolError as exc:
                if exc.code is ErrorCode.TASK_ID_NOT_FOUND:
                    raise ToolError(
                        ErrorCode.PLAN_STALE,
                        f"确认令牌里的任务 {task_id} 已不存在，请重新发起操作",
                    ) from exc
                raise
            task = (data or {}).get("task") or {}
            current = task.get("version")
            if current != expected:
                raise ToolError(
                    ErrorCode.PLAN_STALE,
                    f"任务 {task_id} 已被修改（预览时 version={expected}，当前={current}），请重新预览后再确认",
                )

        ctx.trace.add("GUARD", "guard.confirm_versions_ok", detail={"tasks": list(pending.versions)})

    async def _build_preview(self, tool_name: str, args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
        """给用户看的 diff / 快照。"""
        if tool_name == "task.delete":
            task_id = str(args.get("task_id") or "")
            snapshot: dict[str, Any] = {"id": task_id}
            try:
                data = await ctx.core.request(
                    "GET", f"/api/v1/tasks/{task_id}", trace_id=ctx.trace_id, tool_name="task.delete"
                )
                task = (data or {}).get("task") or {}
                snapshot = {
                    "id": task.get("id", task_id),
                    "title": task.get("title"),
                    "status": (task.get("status") or "").lower(),
                    "dueAt": task.get("dueAt"),
                }
            except ToolError:
                pass
            return {
                "kind": "delete",
                "moved": [],
                "kept": [],
                "cannotFit": [],
                "snapshot": snapshot,
                "message": f"即将删除任务「{snapshot.get('title') or task_id}」，删除后可用恢复接口找回。",
            }

        if tool_name == "task.split":
            children = args.get("children") or []
            return {
                "kind": "split",
                "moved": [],
                "kept": [{"title": c.get("title"), "estimate_min": c.get("estimate_min")} for c in children
                         if isinstance(c, dict)],
                "cannotFit": [],
                "message": f"即将创建 {len(children)} 个子任务。",
            }

        return {
            "kind": tool_name,
            "moved": [],
            "kept": [],
            "cannotFit": [],
            "args": args,
            "message": "该操作会修改多条数据，需要你确认。",
        }

    # ------------------------------------------------------------------
    # 消息组装
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_message: str,
        session: SessionState,
        tier: str,
        ctx: TurnContext,
    ) -> list[dict[str, Any]]:
        system_text, version = system_prompt(tier)
        ctx.prompt_version = version
        ctx.trace.prompt_version = version  # SUMMARY 行里要记下来，否则改 prompt 无法归因
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_text}]

        # 模型不知道"今天"是哪天 —— 不注入的话它会拿训练截止日期算"今天"，
        # 表现为"看看今天的任务"搜出一个几年前的日期区间，然后理直气壮地说没有任务。
        now = self.clock()
        weekday = "一二三四五六日"[now.weekday()]
        messages.append(
            {
                "role": "system",
                "content": (
                    f"【当前时间】{now.isoformat(timespec='minutes')}（星期{weekday}，{now.tzname() or '本地时区'}）。"
                    "用户说的「今天/明天/本周/下周三」都以这个时间为基准计算。"
                ),
            }
        )

        if session.summary:
            messages.append({"role": "system", "content": "【会话摘要】" + session.summary})
        if session.constraints:
            messages.append(
                {"role": "system", "content": "【已确认的约束】" + json.dumps(session.constraints, ensure_ascii=False)}
            )
        for turn in session.recent_turns:
            messages.append({"role": turn.role, "content": turn.content})

        messages.append({"role": "user", "content": user_message})
        return messages

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    async def _emit_text(self, emit: Emit, text: str) -> None:
        if not text:
            return
        for i in range(0, len(text), 24):
            await emit(events.EVENT_TOKEN, {"text": text[i : i + 24]})

    def _finish_outcome(
        self,
        ctx: TurnContext,
        started: float,
        *,
        text: str = "",
        actions_taken: list[str] | None = None,
        clarify: dict[str, Any] | None = None,
        confirm: dict[str, Any] | None = None,
    ) -> TurnOutcome:
        outcome = TurnOutcome(
            text=text,
            actions_taken=actions_taken if actions_taken is not None else list(ctx.actions_taken),
            clarify=clarify,
            confirm=confirm,
            degraded=ctx.degraded,
            degrade_reason=ctx.degrade_reason,
            steps=ctx.budget.steps,
            cost_yuan=round(ctx.budget.cost_yuan, 6),
            tier=ctx.tier,
        )
        return outcome

    def _abort(
        self,
        ctx: TurnContext,
        started: float,
        outcome: TurnOutcome,
        code: ErrorCode,
        message: str,
    ) -> TurnOutcome:
        """超预算 / 打转：**不回滚已执行的动作**，把进度说清楚（ADR D9）。"""
        progress = "、".join(ctx.actions_taken) if ctx.actions_taken else "还没有执行任何修改"
        text = f"{message}。已经完成的：{progress}。剩下的需要你确认后我再继续。"
        outcome.error = ToolError(code, message)
        outcome.text = text
        outcome.actions_taken = list(ctx.actions_taken)
        outcome.steps = ctx.budget.steps
        outcome.cost_yuan = round(ctx.budget.cost_yuan, 6)
        outcome.degraded = ctx.degraded
        ctx.trace.add("GUARD", "guard.abort", ok=False, detail={"code": code.value, "message": message})
        return outcome


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _assistant_message(response: LLMResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.text or None,
        "tool_calls": [
            {
                "id": call.id or f"call_{i}",
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for i, call in enumerate(response.tool_calls)
        ],
    }


def _charge(ctx: TurnContext, response: LLMResponse, settings: Settings) -> None:
    cost = cost_of(settings, response.model or "deepseek-chat", response.tokens_in, response.tokens_out)
    ctx.budget.add_usage(tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_yuan=cost)


def _map_llm_error(exc: LLMError) -> ToolError:
    from ..llm.base import LLMBadOutput, LLMBadRequest, LLMBreakerOpen, LLMRateLimited, LLMTimeout

    if isinstance(exc, LLMRateLimited):
        return ToolError(ErrorCode.RATE_LIMITED, str(exc))
    if isinstance(exc, LLMBreakerOpen):
        return ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "模型服务连续失败，已熔断，稍后再试")
    if isinstance(exc, LLMTimeout):
        return ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "模型响应超时")
    if isinstance(exc, LLMBadOutput):
        return ToolError(ErrorCode.VALIDATION_ERROR, "模型输出格式不合法")
    if isinstance(exc, LLMBadRequest):
        return ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, f"模型请求被拒绝：{exc}")
    return ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, str(exc))


def _referenced_task_ids(args: dict[str, Any]) -> list[str]:
    """只收集本次确认真正引用到的已有任务 id。

    令牌里的 versions 如果塞进全部 visible 任务，任何一个无关任务被改都会让确认
    变成 PLAN_STALE；因此只绑 task_id / parent_id 这两个可能带版本的字段。
    """
    ids: list[str] = []
    for key in ("task_id", "parent_id"):
        value = args.get(key)
        if isinstance(value, str) and value:
            ids.append(value)
    return ids


def _affected_count(tool_name: str, parsed: Any) -> int:
    if tool_name == "task.split":
        children = getattr(parsed, "children", None) or []
        return len(children)
    ids = validators.collect_ids(parsed.model_dump() if hasattr(parsed, "model_dump") else {})
    return max(1, len(ids))


def _summarize(tool_name: str, data: Any) -> str:
    if not isinstance(data, dict):
        return f"{tool_name} 返回 {type(data).__name__}"
    if "items" in data:
        return f"找到 {len(data['items'])} 条，共 {data.get('total', '?')} 条"
    if "task" in data:
        title = (data["task"] or {}).get("title", "")
        return f"{tool_name}：{title}"
    if "windows" in data:
        return f"忙碌 {data.get('count', 0)} 段，共 {data.get('busy_minutes', 0)} 分钟"
    if data.get("control") == "finish":
        return "给出最终回复"
    if data.get("control") == "clarify":
        return "向用户提问"
    if data.get("deleted"):
        return "已删除"
    if "created" in data:
        return f"创建 {data.get('count', 0)} 条"
    return tool_name


def _truncate(data: Any, settings: Settings) -> Any:
    """列表类返回必须截断 + has_more（P1）。"""
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        limit = settings.tool_result_max_items
        items = data["items"]
        if len(items) > limit:
            return {**data, "items": items[:limit], "has_more": True, "truncated": True}
    return data
