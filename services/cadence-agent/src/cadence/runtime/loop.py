"""★ 多步循环状态机 + 三层降级链。

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

---

## L2 降级链（三层，逐层可见）

    L1 换策略：reason 超时/熔断 → 换 fast client 重做本轮（prompt 换成短版，其余参数不变）
        │ 还失败
        ▼
    L2 单步模式：不再规划，只做「1 次工具调用 + 1 次措辞」
        │ 还失败 / Core 挂了
        ▼
    L3 兜底：UPSTREAM_UNAVAILABLE，保留已完成的 actions，绝不假装成功

三条铁律：

1. **降级不是重试**。重试是"同一策略再来一次"（`llm/retry.py` 的退避）；
   降级是"换一个更便宜的策略把话说完"。两者混用会让 OPEN 期间还在打 provider。
2. **每一次降级都必须可见**：trace 里一行 `kind=DEGRADE`（带 reason），
   SSE 里一个 error 事件，最终 `done` 带 `degraded/degradeReason`。
   静默降级等于让用户以为拿到了完整能力。
3. **L3 不假装成功**。Core 连不上时保留已经写成功的 `actions_taken`，
   把"哪一步没做"说清楚 —— 这是 `_abort` 与 `_finish_outcome` 的区别。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..brain import intent as brain_intent
from ..config import Settings
from ..guard import injection, permissions, validators
from ..llm.base import LLMBreakerOpen, LLMClient, LLMError, LLMResponse, LLMTimeout
from ..llm.cost import cost_of
from ..llm.retry import Breaker, call_with_resilience
from ..llm.router import route
from ..promptlib import system_prompt
from ..runtime.context import TurnContext
from ..runtime.errors import BudgetExceeded, ErrorCode, ToolError
from ..runtime.fingerprint import FingerprintTracker
from ..runtime.messages import humanize, humanize_event, redact_protocol_text
from ..runtime.session import SessionState, compress_messages, estimate_messages
from ..tools.base import Tool, ToolResult
from ..tools.registry import ToolRegistry
from . import events

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

#: L2 单步降级里唯一允许的"收尾工具"（没有它就只能靠自然语言收尾）
SINGLE_STEP_TOOL = "meta.finish"


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


@dataclass
class LLMCallContext:
    """一次"模型调用"的全部可变量。

    `client` 与 `tier` 分开：L1 降级换的是 client（同 provider 换模型/提示），
    tier 只决定用长提示还是短提示。两者绑在一起，"换 client 但保持 fast 提示"
    这种组合就表达不出来 —— 而单 provider 下降级恰恰就是这个组合。
    """

    client: LLMClient
    tier: str


@dataclass
class Degradation:
    """降级结果。`ctx` 非空 = 还能继续；`stop` 非空 = 必须停下（L3）。"""

    ctx: LLMCallContext | None = None
    stop: ToolError | None = None


@dataclass
class PreparedCall:
    """一次工具调用的前置结论（主循环与单步降级共用同一套护栏）。"""

    status: str  # act / skip / clarify / confirm / deny / abort
    tool: Tool | None = None
    parsed: Any = None
    affected: int = 0
    error: ToolError | None = None
    clarify: dict[str, Any] | None = None
    confirm: dict[str, Any] | None = None


# ---------------------------------------------------------------------------


class Loop:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: ToolRegistry,
        llm: LLMClient,
        fast_llm: LLMClient | None = None,
        breaker: Breaker | None = None,
        llm_semaphore: asyncio.Semaphore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.llm = llm
        # 单 provider（ASSUMED D4）：fast 通常是同一个 client，只换提示；
        # 但装配层可以传一个真·快模型进来，L1 降级会优先用它。
        self.fast_llm = fast_llm or llm
        self.breaker = breaker
        # L3-2：全局 LLM 信号量。**只在 provider 调用点（叶子）持有**，
        # 不在会话锁之外任何地方取，也不在持有它的时候去拿别的锁 —— 资源图单向。
        self.llm_semaphore = llm_semaphore
        # 时间基准必须注入（PRD 02 §3.3）：不注入，"今天/下周三"在测试里就没法固定
        self.clock = clock or (lambda: datetime.now().astimezone())

    # ------------------------------------------------------------------
    # 入口
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
        started = time.perf_counter()

        # ---- CLASSIFY（路由 + 意图 + 歧义）------------------------------
        decision = route(user_message)
        chosen_tier = tier or decision.tier.value
        # 双模型按 tier 选 client：reason 用 reasoning 模型（deepseek-flash），
        # fast 用非 reasoning 别名（deepseek-chat，更快更便宜）。
        # 显式传入 llm（测试/降级）时优先用它；否则按 tier 选。
        if llm is not None:
            client = llm
        else:
            client = self.fast_llm if chosen_tier == "fast" else self.llm
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
                "question": redact_protocol_text(classify.question),
                "options": [],
                "missing": classify.missing,
            }
            await emit(events.EVENT_CLARIFY, payload)
            ctx.trace.add("GUARD", "guard.clarify", detail=payload)
            session.pending_confirm = None
            return self._finish_outcome(ctx, started, clarify=payload)

        # ---- ACT / OBSERVE 循环 ----------------------------------------
        fingerprints = FingerprintTracker()
        call_ctx = LLMCallContext(client=client, tier=chosen_tier)
        finish_message = ""

        while True:
            if not ctx.budget.can_start_step():
                ctx.trace.add(
                    "DEGRADE",
                    "budget.step_limit",
                    ok=False,
                    detail={"max_steps": ctx.budget.max_steps, "history": ctx.budget.history},
                )
                return self._abort(ctx, started, ErrorCode.BUDGET_EXCEEDED,
                                   f"步数预算用尽（上限 {ctx.budget.max_steps} 步）")

            try:
                ctx.step_index = ctx.budget.start_step()
            except BudgetExceeded as exc:
                return self._abort(ctx, started, exc.code, exc.message)

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
                requested_tier = call_ctx.tier
                response, call_ctx = await self._invoke_with_degrade(
                    call_ctx, messages, ctx, emit
                )
                if call_ctx.tier != requested_tier:
                    # ctx.tier 记的是**路由决定**（评测要看它）；这里把**实际用上的 tier**
                    # 也写进 trace，降级答案的能力边界才可归因。
                    ctx.trace.add(
                        "DEGRADE", "llm.effective_tier", ok=False,
                        detail={"requested": requested_tier, "used": call_ctx.tier},
                    )
                    self._sync_tier(ctx, call_ctx.tier)
            except BudgetExceeded as exc:
                return self._abort(ctx, started, exc.code, exc.message)
            except _LLMFailed as failure:
                # L2 也失败了 → L3 兜底：不假装成功，保留已完成动作
                human, _suggestions = humanize(failure.error)
                return self._abort(ctx, started, failure.error.code, human,
                                   detail_error=failure.error)
            llm_ms = int((time.perf_counter() - llm_started) * 1000)

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
                    "tier": call_ctx.tier,
                    # provider 可能把请求的 model 映射成别的名字（如 chat -> flash），
                    # 这里单独记"我请求的是哪个模型"，评测才能验证路由真的换了模型。
                    "requested_model": getattr(call_ctx.client, "model", "") or "",
                    "model": response.model,
                    "tool_calls": [c.name for c in response.tool_calls],
                    "text_len": len(response.text or ""),
                    # 思考过程：DeepSeek 非 reasoning 模型只返回正文；这里保留前 500 字，
                    # 让评测报告能还原"模型在决定调工具前说了什么"。
                    "text": (response.text or "")[:500],
                    "reasoning": (response.reasoning or "")[:1200] or None,
                    # 该步可见的 id 白名单（由本轮 task.search 结果累积）。
                    "visible_ids": sorted(ctx.visible_task_ids)[:50],
                },
            )

            # 无工具调用 = 模型直接回答，本轮结束
            if not response.tool_calls:
                finish_message = response.text or ""
                if not self.settings.sse_live_tokens:
                    await self._emit_text(emit, finish_message)
                return self._finish_outcome(ctx, started, text=finish_message)

            messages.append(_assistant_message(response))

            for call in response.tool_calls:
                ctx.tool_calls += 1
                prepared = await self._prepare_call(call, ctx, emit, fingerprints)
                stop = await self._apply_prepared(
                    prepared, call, messages, ctx, session, emit, started
                )
                if stop is not None:
                    return stop
                if prepared.status == "act":
                    assert prepared.tool is not None
                    result = await self._execute(prepared.tool, prepared.parsed, ctx)
                    if self._is_core_down(result.error):
                        human, _suggestions = humanize(result.error)  # type: ignore[arg-type]
                        return self._abort(ctx, started, ErrorCode.UPSTREAM_UNAVAILABLE,
                                           human, detail_error=result.error, emit=emit)
                    if isinstance(result.data, dict) and result.data.get("control") == "finish":
                        early = await self._finish_from_tool(result, ctx, emit, started)
                        if early is None:  # pragma: no cover - 正常路径总会返回 outcome
                            continue
                        return early
                    if isinstance(result.data, dict) and result.data.get("control") == "clarify":
                        return await self._clarify_from_tool(result, ctx, session, emit, started)
                    await self._publish_tool_result(result, call, messages, ctx, emit)

                # ---- OBSERVE：预算复核 ----
                if ctx.budget.tokens_total > ctx.budget.max_tokens:
                    return self._abort(ctx, started, ErrorCode.BUDGET_EXCEEDED, "token 预算用尽")

    def _sync_tier(self, ctx: TurnContext, tier: str) -> None:
        ctx.tier = tier

    # ------------------------------------------------------------------
    # 降级链：一次带 L1/L2/L3 的模型调用
    # ------------------------------------------------------------------

    async def _invoke_with_degrade(
        self,
        call_ctx: LLMCallContext,
        messages: list[dict[str, Any]],
        ctx: TurnContext,
        emit: Emit,
    ) -> tuple[LLMResponse, LLMCallContext]:
        """返回 (response, 实际使用的 call_ctx)。失败到 L3 时抛 `_LLMFailed`。"""
        try:
            response = await self._call_llm(call_ctx.client, messages, ctx, emit, call_ctx.tier)
            _charge(self.settings, ctx, response)
            return response, call_ctx
        except _LLMFailed:
            raise
        except LLMError as first:
            error = _map_llm_error(first)

            # F1：模型输出非法 JSON 不算"上游不可用"，降级为一句人话比弹错误码有用。
            from ..llm.base import LLMBadOutput

            if isinstance(first, LLMBadOutput):
                ctx.trace.add("DEGRADE", "llm.bad_output", ok=False, detail={"error": str(first)[:200]})
                return LLMResponse(text="我刚才没太理解，能不能换个说法再说一次？", model=call_ctx.client.name), call_ctx

            # ---- L1：换 fast 重做本轮（只换 client / 提示，其余参数不变）----
            if call_ctx.tier == "reason" and not ctx.degraded:
                call_ctx = await self._degrade_l1(call_ctx, ctx, emit, first)
                try:
                    response = await self._call_llm(call_ctx.client, messages, ctx, emit, call_ctx.tier)
                    _charge(self.settings, ctx, response)
                    return response, call_ctx
                except LLMError as second:
                    error = _map_llm_error(second)
                    first = second
                    ctx.trace.add(
                        "DEGRADE", "llm.degrade_l1_failed", ok=False,
                        detail={"error": str(second)[:200]},
                    )

            # ---- L2：单步模式（不再规划）----
            degraded_ctx = await self._degrade_l2(ctx, emit, first)
            try:
                response = await self._single_step(degraded_ctx.client, messages, ctx, emit)
                _charge(self.settings, ctx, response)
                return response, degraded_ctx
            except _LLMFailed:
                raise
            except Exception as unexpected:  # noqa: BLE001 - 降级路径也不许把异常漏到 API 层
                ctx.trace.add(
                    "DEGRADE", "llm.degrade_l2_failed", ok=False,
                    detail={"error": f"{type(unexpected).__name__}: {unexpected}"[:200]},
                )
                error = _map_llm_error(_as_llm_error(unexpected))

            # ---- L3：全部策略失败 ----
            ctx.trace.add(
                "LLM", "llm.failed", ok=False,
                detail={"code": error.code.value, "error": str(first)[:300]},
            )
            await emit(events.EVENT_ERROR, humanize_event(error))
            raise _LLMFailed(error) from first

    async def _degrade_l1(
        self,
        call_ctx: LLMCallContext,
        ctx: TurnContext,
        emit: Emit,
        error: Exception,
    ) -> LLMCallContext:
        ctx.mark_degraded(f"reason_model_unavailable:{type(error).__name__}")
        ctx.trace.add(
            "DEGRADE", "llm.degrade_l1", ok=False,
            detail={"from": call_ctx.tier, "to": "fast", "error": str(error)[:200]},
        )
        await emit(
            events.EVENT_ERROR,
            {
                "code": "DEGRADED",
                "message": "推理模型不可用，已切换到快速模式，任务分解能力受限",
                "degraded": True,
                "suggestions": [],
            },
        )
        return LLMCallContext(client=self.fast_llm, tier="fast")

    async def _degrade_l2(self, ctx: TurnContext, emit: Emit, error: Exception) -> LLMCallContext:
        ctx.mark_degraded(f"single_step_fallback:{type(error).__name__}")
        ctx.trace.add(
            "DEGRADE", "llm.degrade_l2", ok=False,
            detail={"mode": "single_step", "max_tool_calls": 1, "error": str(error)[:200]},
        )
        await emit(
            events.EVENT_ERROR,
            {
                "code": "DEGRADED",
                "message": "完整规划模式不可用，已切到单步模式：这轮只做一步，可能有遗漏",
                "degraded": True,
                "suggestions": [{"hint": "拆成一句话一件事再让我做", "reason": "单步模式无法规划多步"}],
            },
        )
        return LLMCallContext(client=self.fast_llm, tier="fast")

    async def _single_step(
        self,
        client: LLMClient,
        messages: list[dict[str, Any]],
        ctx: TurnContext,
        emit: Emit,
    ) -> LLMResponse:
        """L2：一次工具调用 + 一次措辞，不再规划。

        - 只执行**第一个**工具调用（后面的直接丢弃），坚决不进入多步循环；
        - 护栏照旧（权限 / 参数 / id 白名单 / 需确认的破坏性操作）；
        - 需要确认的破坏性操作在单步模式下**不执行**，改成问用户 ——
          "不规划"不等于"跳过确认"。

        返回的 `LLMResponse` 会被主循环当成"模型这一步的回答"：
        无 tool_calls → 直接收尾，所以不会再规划第二轮。
        """
        async def once() -> LLMResponse:
            return await self._stream_once(client, messages, emit)

        # 熔断 OPEN 期间**不打 provider**（L2 硬约束）。降级也不能绕开熔断：
        # 单步模式只是换策略，不是换一个不受熔断约束的通道。
        if self.breaker is not None and not self.breaker.allow():
            ctx.trace.add("DEGRADE", "llm.degrade_l2_breaker_open", ok=False, detail={})
            raise LLMBreakerOpen("熔断器打开，单步降级也不发起请求")

        response = await call_with_resilience(once, breaker=None, times=0)

        if not response.tool_calls:
            ctx.trace.add(
                "GUARD", "guard.single_step", detail={"tool_calls": 0, "executed": 0},
            )
            return response

        call = response.tool_calls[0]
        call.name = self.registry.logical_name(call.name)
        ctx.tool_calls += 1
        ctx.trace.add(
            "GUARD", "guard.single_step",
            detail={
                "tool_calls": len(response.tool_calls),
                "executed": 1,
                "tool": call.name,
                "dropped": [c.name for c in response.tool_calls[1:]],
            },
        )

        prepared = await self._prepare_call(call, ctx, emit, FingerprintTracker())
        if prepared.status in ("clarify", "confirm"):
            # 信息不全 / 需要确认：单步模式停下来问，绝不"降级即放行"。
            payload = prepared.clarify or prepared.confirm or {}
            question = payload.get("question") or payload.get("plan", {}).get("message") or "这一步需要你确认后再做。"
            return LLMResponse(text=question, model=response.model,
                               tokens_in=response.tokens_in, tokens_out=response.tokens_out)
        if prepared.status == "abort":
            assert prepared.error is not None
            raise _LLMFailed(prepared.error)
        if prepared.status == "skip":
            assert prepared.error is not None
            await self._emit_failure(emit, call.name, prepared.error)
            return LLMResponse(text=prepared.error.message, model=response.model,
                               tokens_in=response.tokens_in, tokens_out=response.tokens_out)

        assert prepared.tool is not None
        result = await self._execute(prepared.tool, prepared.parsed, ctx)
        if self._is_core_down(result.error):
            raise _LLMFailed(result.error)  # type: ignore[arg-type]

        control = result.data if isinstance(result.data, dict) else {}
        if control.get("control") == "finish":
            text = control.get("message") or ""
        elif control.get("control") == "clarify":
            text = control.get("question") or "这一步还需要你补充信息。"
        else:
            text = result.summary or "这一步做完了。"
        return LLMResponse(text=text, model=response.model,
                           tokens_in=response.tokens_in, tokens_out=response.tokens_out)

    # ------------------------------------------------------------------
    # LLM 调用：超时 + 熔断 + 重试
    # ------------------------------------------------------------------

    async def _call_llm(
        self,
        client: LLMClient,
        messages: list[dict[str, Any]],
        ctx: TurnContext,
        emit: Emit,
        tier: str,
    ) -> LLMResponse:
        async def once() -> LLMResponse:
            return await self._stream_once(client, messages, emit)

        return await call_with_resilience(
            once,
            breaker=self.breaker,
            times=self.settings.llm_retry_times,
            base_delay=self.settings.llm_retry_backoff_s,
        )

    async def _stream_once(
        self, client: LLMClient, messages: list[dict[str, Any]], emit: Emit
    ) -> LLMResponse:
        """一次 provider 调用：**先拿信号量（叶子），再套读超时**。

        顺序不能反：先超时后排队的话，"等信号量"的时间会被算进 read 超时，
        50 并发下会把本来正常的调用误判成超时。
        读超时放在这一次调用内（不是整个重试循环外），这样超时也能按 L2 的策略重试 1 次。
        """
        async with self._llm_slot():
            try:
                async with asyncio.timeout(self.settings.llm_read_timeout_s):
                    return await self._consume_stream(client, messages, emit)
            except TimeoutError as exc:
                raise LLMTimeout(f"模型读超时（>{self.settings.llm_read_timeout_s:.0f}s）") from exc

    @asynccontextmanager
    async def _llm_slot(self):
        """LLM 信号量的唯一获取点。没有注入信号量时退化为"不限并发"（单测用）。"""
        if self.llm_semaphore is None:
            yield
            return
        async with self.llm_semaphore:
            yield

    async def _consume_stream(
        self, client: LLMClient, messages: list[dict[str, Any]], emit: Emit
    ) -> LLMResponse:
        async for ev in client.stream(messages, self.registry.openai_tools()):
            if ev.kind == "token":
                # 默认缓冲模式：不在这里推，等这轮确认成功由调用方一次性推，
                # 避免"流到一半失败 → 重试 → 文本重复"。
                # 打开 SSE_LIVE_TOKENS 时才边生成边推（体验好，但可能重复一段）。
                if self.settings.sse_live_tokens:
                    await emit(events.EVENT_TOKEN, {"text": ev.text})
            elif ev.kind == "done" and ev.response is not None:
                return ev.response
        raise LLMError("流式响应没有 done 事件")

    def _settings_for(self, ctx: TurnContext) -> Settings:
        return ctx.settings

    # ------------------------------------------------------------------
    # 工具调用：前置护栏 + 执行 + 结果发布（主循环与单步共用）
    # ------------------------------------------------------------------

    async def _prepare_call(
        self,
        call: Any,
        ctx: TurnContext,
        emit: Emit,
        fingerprints: FingerprintTracker,
    ) -> PreparedCall:
        # ---- 指纹去重（F5）----
        stuck, fp = fingerprints.is_stuck(call.name, call.arguments)
        if stuck:
            ctx.trace.add(
                "GUARD", "guard.fingerprint", ok=False,
                detail={"fingerprint": fp, "tool": call.name},
            )
            return PreparedCall(
                status="abort",
                error=ToolError(
                    ErrorCode.LOOP_DETECTED,
                    f"检测到重复调用 {call.name}（同一参数连续两次），中断并汇报进度",
                ),
            )

        tool = self.registry.get(call.name)
        # 先报"要调用了"，再判断拦不拦 —— 前端时间线上要能看到"尝试过但被拦下"
        await emit(
            events.EVENT_TOOL_CALL,
            {"name": call.name, "args": call.arguments, "step": ctx.step_index},
        )

        if tool is None:
            return PreparedCall(
                status="skip",
                error=ToolError(
                    ErrorCode.VALIDATION_ERROR,
                    f"没有名为 {call.name} 的工具。可用工具：{', '.join(self.registry.names())}",
                ),
            )

        # ---- 权限 ----
        try:
            permissions.assert_executable(call.name)
        except permissions.PermissionDenied as exc:
            return PreparedCall(status="skip", error=ToolError(ErrorCode.NOT_IMPLEMENTED, str(exc)))

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
            return PreparedCall(status="clarify", clarify=payload)

        # ---- id 白名单（F6）----
        try:
            validators.assert_ids_visible(call.arguments, ctx.visible_task_ids, tool_name=call.name)
        except ToolError as exc:
            ctx.trace.add(
                "GUARD", "guard.whitelist", ok=False,
                detail={"tool": call.name, "offending": (exc.details or {}).get("offending_id")},
            )
            return PreparedCall(status="skip", error=exc)

        # ---- 危险操作确认（F7）----
        affected = _affected_count(call.name, parsed)
        if permissions.needs_confirm(
            call.name, affected=affected, batch_threshold=self.settings.destructive_batch_threshold
        ):
            try:
                ok, pending_payload = await self._handle_confirm(call, tool, ctx, emit, affected)
            except ToolError as exc:
                # 令牌过期/版本变化/作用域不符：不执行目标工具，把 PLAN_STALE 作为本轮结果。
                return PreparedCall(status="abort", error=exc)
            if not ok:
                return PreparedCall(
                    status="confirm",
                    tool=tool,
                    parsed=parsed,
                    affected=affected,
                    confirm=pending_payload,
                )

        return PreparedCall(status="act", tool=tool, parsed=parsed, affected=affected)

    async def _apply_prepared(
        self,
        prepared: PreparedCall,
        call: Any,
        messages: list[dict[str, Any]],
        ctx: TurnContext,
        session: SessionState,
        emit: Emit,
        started: float,
    ) -> TurnOutcome | None:
        """把前置结论落成事件/消息；返回 None 表示继续执行，非 None 表示本轮到此为止。"""
        if prepared.status == "act":
            return None

        if prepared.status == "skip":
            assert prepared.error is not None
            await self._append_tool_message(
                messages, call.id, prepared.error.to_tool_json(), ctx, call.name, prepared.error, 0
            )
            await self._emit_failure(emit, call.name, prepared.error)
            return None

        if prepared.status == "clarify":
            assert prepared.clarify is not None
            payload = dict(prepared.clarify)
            payload["question"] = redact_protocol_text(payload.get("question") or "")
            await emit(events.EVENT_CLARIFY, payload)
            session.pending_confirm = None
            return self._finish_outcome(ctx, started, clarify=payload)

        if prepared.status == "confirm":
            assert prepared.confirm is not None
            session.pending_confirm = prepared.confirm
            return self._finish_outcome(ctx, started, confirm=prepared.confirm)

        if prepared.status == "abort":
            assert prepared.error is not None
            human, _suggestions = humanize(prepared.error)
            if prepared.error.code in (ErrorCode.NOT_IMPLEMENTED, ErrorCode.UPSTREAM_UNAVAILABLE):
                return self._abort(ctx, started, prepared.error.code, human,
                                   detail_error=prepared.error, emit=emit)
            return self._abort(ctx, started, prepared.error.code, prepared.error.message,
                               detail_error=prepared.error)

        raise AssertionError(f"未知的前置状态 {prepared.status}")

    async def _finish_from_tool(
        self, result: ToolResult, ctx: TurnContext, emit: Emit, started: float
    ) -> TurnOutcome | None:
        """`meta.finish` 的收尾：核对 actions_taken 是否真实，然后结束本轮。"""
        data = result.data if isinstance(result.data, dict) else {}
        finish_message = data.get("message") or ""
        unverified = data.get("unverified") or []
        if unverified:
            ctx.trace.add("GUARD", "guard.actions_unverified", ok=False, detail={"claims": unverified})
            finish_message = (
                finish_message
                + "\n\n（注意：其中有些动作我其实没有执行成功，请以任务列表实际状态为准。）"
            )
        await self._emit_text(emit, finish_message)
        return self._finish_outcome(
            ctx, started, text=finish_message, actions_taken=list(ctx.actions_taken)
        )

    async def _clarify_from_tool(
        self, result: ToolResult, ctx: TurnContext, session: SessionState, emit: Emit, started: float
    ) -> TurnOutcome:
        data = result.data if isinstance(result.data, dict) else {}
        payload = {
            "control": "clarify",
            "question": redact_protocol_text(data.get("question") or ""),
            "options": data.get("options") or [],
            "missing": data.get("missing") or [],
        }
        await emit(events.EVENT_CLARIFY, payload)
        session.pending_confirm = None
        return self._finish_outcome(ctx, started, clarify=payload)

    async def _publish_tool_result(
        self,
        result: ToolResult,
        call: Any,
        messages: list[dict[str, Any]],
        ctx: TurnContext,
        emit: Emit,
    ) -> None:
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
        # 501 / 503 不只是在模型上下文里消化掉：用户也要立刻看到一句人话，
        # 否则"这个能力没实现"会变成模型的一句含糊其辞。码保留给机器，文案换成人话。
        if result.error is not None and result.error.code in (
            ErrorCode.NOT_IMPLEMENTED,
            ErrorCode.UPSTREAM_UNAVAILABLE,
        ):
            await emit(events.EVENT_ERROR, humanize_event(result.error))

    @staticmethod
    def _is_core_down(error: ToolError | None) -> bool:
        """L3 的判定条件：**Core 这个依赖挂了**，不是"这次工具调用错了"。

        `core_down` 由 CoreClient 在传输层失败/5xx 时打标；业务错误（409/422/404）
        仍然只是喂回模型让它自纠，绝不能把整轮打成"上游不可用"。
        """
        return bool(error is not None and error.core_down)

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------

    async def _execute(self, tool: Tool, parsed: Any, ctx: TurnContext) -> ToolResult:
        started = time.perf_counter()
        trace_args = _trace_args(parsed)
        try:
            data = await tool.handler(parsed, ctx)
        except ToolError as exc:
            duration = int((time.perf_counter() - started) * 1000)
            ctx.trace.add(
                "TOOL", tool.name, duration_ms=duration, ok=False,
                detail={"code": exc.code.value, "error": exc.message, "core_down": exc.core_down,
                        "args": trace_args},
            )
            return ToolResult(ok=False, error=exc, summary=exc.message, duration_ms=duration,
                              model_payload=exc.to_tool_json())
        except Exception as exc:  # noqa: BLE001 - 任何未预期异常都要变成结构化错误
            duration = int((time.perf_counter() - started) * 1000)
            wrapped = ToolError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"工具 {tool.name} 执行失败：{type(exc).__name__}: {exc}",
                # 工具 handler 里没被翻译的异常，绝大多数是 I/O（Core 挂了/连接重置）。
                # 标成 core_down：L3 只在"依赖不可用"时打断整轮，而不是每个工具错误都打断。
                core_down=True,
            )
            ctx.trace.add("TOOL", tool.name, duration_ms=duration, ok=False,
                          detail={"code": wrapped.code.value, "error": str(exc)[:300],
                                  "args": trace_args})
            return ToolResult(ok=False, error=wrapped, summary=wrapped.message, duration_ms=duration,
                              model_payload=wrapped.to_tool_json())

        duration = int((time.perf_counter() - started) * 1000)
        summary = _summarize(tool.name, data)
        ctx.trace.add("TOOL", tool.name, duration_ms=duration, ok=True,
                      detail={"summary": summary, "args": trace_args,
                              "visible_ids": sorted(ctx.visible_task_ids)[:50]})
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

        ⚠️ 这是一次 **Core 读**，必须走 Core 的超时矩阵（connect 2s / read 3s）。
        历史上这里曾经复用 LLM 的 15s 读超时：Core 挂掉时确认流程会白等 15 秒，
        而且超时错误会被误判成"模型侧问题"。现在由 `ctx.core.request` 统一保证。
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
        # 所有面向用户的文本都从这里出去：顺手抹掉协议码（NOT_IMPLEMENTED / 501 等），
        # 否则模型会把工具错误原样复述给用户（L2-4 明确禁止）。
        clean = redact_protocol_text(text)
        for i in range(0, len(clean), 24):
            await emit(events.EVENT_TOKEN, {"text": clean[i : i + 24]})

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
        return TurnOutcome(
            text=text,
            actions_taken=actions_taken if actions_taken is not None else list(ctx.actions_taken),
            clarify=clarify,
            confirm=confirm,
            degraded=ctx.degraded,
            degrade_reason=ctx.degrade_reason,
            steps=ctx.budget.steps,
            cost_yuan=ctx.budget.cost_yuan,
            tier=ctx.tier,
        )

    def _abort(
        self,
        ctx: TurnContext,
        started: float,
        code: ErrorCode,
        message: str,
        *,
        detail_error: ToolError | None = None,
        emit: Emit | None = None,
    ) -> TurnOutcome:
        """超预算 / 打转 / 上游不可用：**不回滚已执行的动作**，把进度说清楚（ADR D9）。"""
        progress = "、".join(ctx.actions_taken) if ctx.actions_taken else "还没有执行任何修改"
        text = f"{message}。已经完成的：{progress}。剩下的需要你确认后我再继续。"
        error = detail_error or ToolError(code, message)
        ctx.trace.add("GUARD", "guard.abort", ok=False, detail={"code": code.value, "message": message[:300]})
        outcome = TurnOutcome(
            text=text,
            actions_taken=list(ctx.actions_taken),
            error=error,
            degraded=ctx.degraded,
            degrade_reason=ctx.degrade_reason,
            steps=ctx.budget.steps,
            cost_yuan=ctx.budget.cost_yuan,
            aborted=code.value,
            tier=ctx.tier,
        )
        return outcome


# ---------------------------------------------------------------------------
# 内部信号
# ---------------------------------------------------------------------------


class _LLMFailed(Exception):
    """L1/L2 全部失败，或 L3 判定成立（Core 不可用），携带已翻译好的 ToolError。"""

    def __init__(self, error: ToolError) -> None:
        super().__init__(error.message)
        self.error = error


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


def _charge(settings: Settings, ctx: TurnContext, response: LLMResponse) -> None:
    cost = cost_of(settings, response.model or "deepseek-chat", response.tokens_in, response.tokens_out)
    ctx.budget.add_usage(tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_yuan=cost)


def _as_llm_error(exc: Exception) -> LLMError:
    """降级路径里的意外异常统一成 LLMError，保证 L3 仍能给出结构化错误。"""
    if isinstance(exc, LLMError):
        return exc
    return LLMError(f"{type(exc).__name__}: {exc}")


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
            return {**data, **{"items": items[:limit], "has_more": True, "truncated": True}}
    return data


def _trace_args(parsed: Any) -> dict[str, Any]:
    """把工具入参写进 trace 的 `detail.args`（L4 的 name/arg 指标要读它）。

    两个约束：
    1. `json.dumps` 必须能序列化 —— pydantic 的 `datetime` 先转 ISO 字符串；
    2. 令牌类字段不落盘 —— `confirm_token` / `idempotency_key` 对评测无用，没必要留。
    """
    raw = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
    if not isinstance(raw, dict):
        return {}
    return _jsonable(raw)  # type: ignore[return-value]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(k): _jsonable(v)
            for k, v in value.items()
            if k not in {"confirm_token", "idempotency_key"}
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
