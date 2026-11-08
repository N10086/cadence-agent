"""故障注入：把 5 类上游故障打进真实循环，验证"降级而不是崩"。

为什么要有这个脚本，而不是只写 pytest：

- 单测断言的是**函数**，这里断言的是**一轮对话的结局**（文本 / 降级标记 / trace / 不烧钱）。
- `--mode mock` 完全不依赖真 Core、真 provider、真网络：故障从 client 层注入，
  Core 用内存版。所以它能在断网、没 key、CI 里跑。
- 输出是给人看的 `PASS <场景>` / `FAIL <场景> <原因>`，退出码是判定依据。

判定口径（与 L2 契约一致）：

| 场景 | 必须发生的事 |
|---|---|
| 429 | 不做同策略重试（不放大计费）→ 走降级链，最终可见 |
| timeout | reason 超时 → L1 换 fast 成功；trace 有 DEGRADE |
| 5xx | provider 连续 5xx → L1/L2 都失败 → L3：UPSTREAM_UNAVAILABLE，不假装成功 |
| core-down | Core 传输失败 → UPSTREAM_UNAVAILABLE + 人话 + 保留已完成动作 |
| sse-drop | 流断在半截（没有 done）→ 不重复半截文本，走降级链收尾 |

用法：

    python scripts/fault.py --mode mock --scenario all
    python scripts/fault.py --mode mock --scenario timeout
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

REPO = Path(__file__).resolve().parents[1]
AGENT = REPO / "services" / "cadence-agent"
# src 在前、tests 在后：工具/护栏来自 src，FakeCore 来自 tests/fixtures。
for _p in (AGENT / "src", AGENT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cadence.config import Settings  # noqa: E402
from cadence.deps import Runtime  # noqa: E402
from cadence.llm.base import (  # noqa: E402
    LLMEvent,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    LLMUnavailable,
    ToolCall,
)
from cadence.llm.fake import FakeLLM, call, say  # noqa: E402
from cadence.runtime.budget import Budget  # noqa: E402
from cadence.runtime.context import TurnContext  # noqa: E402
from cadence.runtime.errors import ErrorCode, ToolError  # noqa: E402
from cadence.runtime.loop import Loop  # noqa: E402
from cadence.runtime.trace import TraceRecorder, load_trace  # noqa: E402

from fixtures.fake_core import FakeCore  # noqa: E402


# ---------------------------------------------------------------------------
# 故障 client：只实现 LLMClient 协议，故障在"stream"这一层打进去
# ---------------------------------------------------------------------------


class FaultClient:
    """按顺序吐"剧本回合"的 client，回合可以是错、是慢、是半截流。

    `name` 保留 provider 语义：trace 里能看出是哪个上游坏的。
    """

    def __init__(self, name: str, script: list[Any]) -> None:
        self.name = name
        self._script = list(script)
        self._i = 0
        self.calls = 0

    async def chat(self, messages, tools=None, **kw) -> LLMResponse:  # pragma: no cover - 不走
        raise AssertionError("故障脚本用 stream，不调 chat")

    async def stream(self, messages, tools=None, **kw) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        if self._i >= len(self._script):
            # 剧本耗尽 = 上游仍然坏着。这里绝不能"兜底成功"，
            # 否则 5xx / 断流场景会被一条编出来的文本悄悄救回来，测不出 L3。
            raise LLMUnavailable("模拟：剧本耗尽（上游持续故障）")
        step = self._script[self._i]
        self._i += 1

        if step == "timeout":
            raise LLMTimeout("模拟读超时")
        if step == "429":
            raise LLMRateLimited("模拟 429 限流", retry_after_s=0.0)
        if step == "5xx":
            raise LLMUnavailable("模拟 provider 503")
        if step == "sse_drop":
            # 关键：先推半截文本，然后**不推 done** 就结束 —— 这正是"sse 断流"的形状
            yield LLMEvent(kind="token", text="我查到一半")
            return

        if isinstance(step, str):
            step = say(step)

        if step.get("tool_calls"):
            tool_calls = [
                ToolCall(name=c["name"], arguments=dict(c.get("arguments") or {}))
                for c in step["tool_calls"]
            ]
            yield LLMEvent(
                kind="done",
                response=LLMResponse(text="", tool_calls=tool_calls, tokens_in=10, tokens_out=5, model="fault"),
            )
            return

        text = step.get("text") or ""
        for i in range(0, len(text), 8):
            yield LLMEvent(kind="token", text=text[i : i + 8])
        yield LLMEvent(
            kind="done",
            response=LLMResponse(text=text, tokens_in=10, tokens_out=5, model="fault"),
        )


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    text: str
    error_code: str
    degraded: bool
    degrade_reason: str
    trace_steps: list[tuple[str, str]]
    provider_calls: int
    events: list[tuple[str, dict]]


def _settings(tmp: Path) -> Settings:
    return Settings(
        llm_mode="fake",
        deepseek_api_key="",
        zhipu_api_key="",
        data_dir=str(tmp / "agent"),
        llm_retry_times=1,
        llm_retry_backoff_s=0.0,
        core_retry_times=0,
        core_retry_backoff_s=0.0,
    )


async def _run(
    tmp: Path,
    *,
    primary: FaultClient,
    fast: FaultClient,
    message: str,
    tier: str | None = None,
    core_down: bool = False,
) -> ScenarioResult:
    settings = _settings(tmp)
    core = FakeCore(
        [
            {"id": "t_001", "title": "买牛奶", "status": "TODO", "priority": "NORMAL", "version": 0},
        ]
    )
    if core_down:
        async def broken(*_a: Any, **_kw: Any) -> Any:
            raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "Core 连接失败：Connection refused",
                            core_down=True)

        core.request = broken  # type: ignore[assignment]

    rt = Runtime(settings, llm=primary, core=core)
    rt.loop = Loop(
        settings=settings,
        registry=rt.registry,
        llm=primary,
        fast_llm=fast,
        breaker=rt.breaker,
    )

    trace = TraceRecorder("tr_fault", settings.resolved_trace_dir, session_id="s_fault")
    ctx = TurnContext(
        settings=settings,
        core=core,
        trace=trace,
        confirms=rt.confirms,
        trace_id="tr_fault",
        session_id="s_fault",
        budget=Budget(max_steps=settings.step_budget, max_tokens=settings.token_budget_per_turn),
    )
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    outcome = await rt.loop.run_turn(
        ctx, user_message=message, session=rt.sessions.get("s_fault"), emit=emit, tier=tier
    )
    trace.close(cost_yuan=outcome.cost_yuan, total_ms=0, degraded=outcome.degraded)

    data = load_trace(settings.resolved_trace_dir, "tr_fault") or {}
    steps = [(s["kind"], s["name"]) for s in data.get("steps", [])]
    return ScenarioResult(
        text=outcome.text,
        error_code=outcome.error.code.value if outcome.error is not None else "",
        degraded=outcome.degraded,
        degrade_reason=outcome.degrade_reason,
        trace_steps=steps,
        provider_calls=primary.calls + fast.calls,
        events=events,
    )


def _has_degrade_trace(result: ScenarioResult) -> bool:
    return any(kind == "DEGRADE" for kind, _ in result.trace_steps)


def _has_degrade_event(result: ScenarioResult) -> bool:
    return any(ev == "error" and d.get("degraded") for ev, d in result.events)


# ---- 场景 1：429 ----------------------------------------------------------


async def scenario_429(tmp: Path) -> None:
    primary = FaultClient("provider", ["429", say("限流后的快模式回答")])
    result = await _run(
        tmp, primary=primary, fast=primary, message="帮我准备面试", tier="reason"
    )

    assert result.degraded, "429 必须触发降级"
    assert result.degrade_reason.startswith("reason_model_unavailable"), result.degrade_reason
    assert _has_degrade_trace(result), "trace 里必须有 DEGRADE"
    assert _has_degrade_event(result), "SSE 里必须有 degraded 事件"
    # 429 不做同策略重试：primary 总共只被调 1 次（降级后由 fast 接手）
    assert primary.calls == 2, f"429 不该被重试烧钱（实际调用 {primary.calls} 次）"
    assert result.text == "限流后的快模式回答"


# ---- 场景 2：timeout ------------------------------------------------------


async def scenario_timeout(tmp: Path) -> None:
    # 第 1 次 reason 超时、同策略重试仍超时（llm_retry_times=1），第 3 次 fast 才成功。
    # 这正是 L1 降级的触发条件：同策略救不回来，才换策略。
    primary = FaultClient("provider", ["timeout", "timeout", say("快速模式回答")])
    result = await _run(
        tmp, primary=primary, fast=primary, message="帮我准备面试", tier="reason"
    )

    assert result.degraded
    assert result.degrade_reason.startswith("reason_model_unavailable")
    assert ("DEGRADE", "llm.degrade_l1") in result.trace_steps, trace_names(result)
    assert result.text == "快速模式回答"
    assert result.error_code == "", "降级成功不该带错误码"


# ---- 场景 3：5xx ----------------------------------------------------------


async def scenario_5xx(tmp: Path) -> None:
    primary = FaultClient("provider", ["5xx"])
    # tier=reason 才能走完 L1（换 fast，仍 5xx）→ L2（单步，仍 5xx）→ L3
    result = await _run(tmp, primary=primary, fast=primary, message="看看任务", tier="reason")

    assert result.degraded
    assert result.error_code == ErrorCode.UPSTREAM_UNAVAILABLE.value, result.error_code
    assert "上游" in result.text, result.text
    assert "已经完成的" in result.text, "L3 必须报告已完成的动作，不假装成功"
    assert any(name.endswith("degrade_l2") for _k, name in result.trace_steps), trace_names(result)


# ---- 场景 4：core-down ----------------------------------------------------


async def core_down_scenario(tmp: Path) -> None:
    primary = FaultClient("provider", [call("task.search", limit=5), say("不该走到这里")])
    result = await _run(
        tmp, primary=primary, fast=primary, message="看看任务", core_down=True
    )

    assert result.error_code == ErrorCode.UPSTREAM_UNAVAILABLE.value, result.error_code
    assert "任务服务" in result.text, "Core 挂了要给 Core 的人话，而不是模型的话"
    assert "已经完成的" in result.text
    assert result.text, "不许假装成功（必须有可见的收尾文本）"


# ---- 场景 5：sse-drop -----------------------------------------------------


async def scenario_sse_drop(tmp: Path) -> None:
    primary = FaultClient("provider", ["sse_drop"])
    result = await _run(tmp, primary=primary, fast=primary, message="看看任务", tier="reason")

    assert result.degraded, "断流必须触发降级（不是静默重试）"
    assert result.error_code == ErrorCode.UPSTREAM_UNAVAILABLE.value, result.error_code
    assert "我查到一半" not in result.text, "半截文本绝不能当成完整回答交付"
    assert any(name.endswith("degrade_l2") for _k, name in result.trace_steps), trace_names(result)


SCENARIOS: dict[str, Callable[[Path], Any]] = {
    "429": scenario_429,
    "timeout": scenario_timeout,
    "5xx": scenario_5xx,
    "core-down": core_down_scenario,
    "sse-drop": scenario_sse_drop,
}


def trace_names(result: ScenarioResult) -> str:
    return "trace=" + ",".join(f"{k}:{n}" for k, n in result.trace_steps)


# ---------------------------------------------------------------------------


async def main_async(scenario: str) -> int:
    names = list(SCENARIOS) if scenario == "all" else [scenario]
    # 沙箱里系统临时目录不可删（WinError 5），所以工作目录放回仓库内。
    # 每个场景一个唯一子目录：trace / session 不互相污染，也不依赖外部 tmp。
    run_root = REPO / ".tmp" / "fault"
    run_root.mkdir(parents=True, exist_ok=True)

    failed = 0
    for name in names:
        tmp = run_root / f"{name}-{uuid.uuid4().hex[:8]}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            await SCENARIOS[name](tmp)
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name} {exc}")
        except Exception as exc:  # noqa: BLE001 - 脚本要把任何炸法都变成 FAIL 行
            failed += 1
            print(f"FAIL {name} {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cadence 故障注入（L2-5）")
    parser.add_argument("--mode", choices=["mock"], default="mock",
                        help="mock：完全离线，不依赖真 Core / 真 provider")
    parser.add_argument("--scenario", default="all",
                        help="all | " + " | ".join(SCENARIOS))
    args = parser.parse_args(argv)

    if args.scenario != "all" and args.scenario not in SCENARIOS:
        print(f"FAIL unknown-scenario 未知场景 {args.scenario}")
        return 2

    return asyncio.run(main_async(args.scenario))


if __name__ == "__main__":
    sys.exit(main())
