"""共用夹具。

三条原则：
1. **不打真网络**：Core 用 FakeCore，LLM 用 FakeLLM。
2. **时间可冻结**：Loop 接受 clock，所以"今天/下周三"可以精确断言。
3. **落盘落 tmp**：trace / session 都写 tmp_path，测试之间不串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import pytest

from cadence.config import Settings
from cadence.deps import Runtime
from cadence.llm.fake import FakeLLM
from cadence.runtime.budget import Budget
from cadence.runtime.context import TurnContext
from cadence.runtime.loop import Loop, TurnOutcome
from cadence.runtime.trace import TraceRecorder

from fixtures.fake_core import FakeCore

FROZEN_NOW = datetime(2026, 11, 8, 10, 30, tzinfo=datetime.now().astimezone().tzinfo)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        llm_mode="fake",
        deepseek_api_key="",
        zhipu_api_key="",
        data_dir=str(tmp_path / "agent"),
        step_budget=8,
        token_budget_per_turn=200_000,
        max_context_tokens=4000,
        confirm_ttl_s=300,
        destructive_batch_threshold=3,
        llm_retry_times=1,
        llm_retry_backoff_s=0.0,
        core_retry_times=2,
        core_retry_backoff_s=0.0,
    )


@pytest.fixture
def fixed_clock() -> Callable[[], datetime]:
    return lambda: FROZEN_NOW


def make_tasks() -> list[dict[str, Any]]:
    return [
        {
            "id": "t_001",
            "title": "买牛奶",
            "description": None,
            "status": "TODO",
            "priority": "NORMAL",
            "startAt": None,
            "dueAt": "2026-11-08T18:00:00+08:00",
            "estimateMin": 30,
            "parentId": None,
            "pinned": False,
            "rrule": None,
            "tags": [],
            "version": 2,
            "createdAt": "2026-11-01T09:00:00+08:00",
            "updatedAt": "2026-11-01T09:00:00+08:00",
        },
        {
            "id": "t_002",
            "title": "写周报",
            "description": None,
            "status": "TODO",
            "priority": "HIGH",
            "startAt": None,
            "dueAt": "2026-11-09T18:00:00+08:00",
            "estimateMin": 60,
            "parentId": None,
            "pinned": False,
            "rrule": None,
            "tags": ["work"],
            "version": 1,
            "createdAt": "2026-11-01T09:00:00+08:00",
            "updatedAt": "2026-11-01T09:00:00+08:00",
        },
    ]


@pytest.fixture
def core() -> FakeCore:
    return FakeCore(make_tasks())


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM([])


@pytest.fixture
def runtime(settings: Settings, llm: FakeLLM, core: FakeCore, fixed_clock) -> Runtime:
    rt = Runtime(settings, llm=llm, core=core)
    rt.loop = Loop(settings=settings, registry=rt.registry, llm=llm, breaker=rt.breaker, clock=fixed_clock)
    return rt


@dataclass
class RunResult:
    outcome: TurnOutcome
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    ctx: TurnContext | None = None
    session: Any = None

    def of(self, event: str) -> list[dict[str, Any]]:
        return [d for e, d in self.events if e == event]

    @property
    def text_tokens(self) -> str:
        return "".join(d.get("text", "") for d in self.of("token"))


def make_ctx(rt: Runtime, *, session_id: str = "s_test", trace_id: str = "tr_test") -> TurnContext:
    trace = TraceRecorder(trace_id, rt.settings.resolved_trace_dir, session_id=session_id)
    return TurnContext(
        settings=rt.settings,
        core=rt.core,
        trace=trace,
        confirms=rt.confirms,
        trace_id=trace_id,
        session_id=session_id,
        budget=Budget(max_steps=rt.settings.step_budget, max_tokens=rt.settings.token_budget_per_turn),
    )


async def run_script(
    rt: Runtime,
    script: list[Any],
    message: str,
    *,
    tier: str | None = None,
    session_id: str = "s_test",
) -> RunResult:
    assert isinstance(rt.llm, FakeLLM)
    rt.llm.load(script)
    ctx = make_ctx(rt, session_id=session_id)
    session = rt.sessions.get(session_id)
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append((event, data))

    outcome = await rt.loop.run_turn(
        ctx,
        user_message=message,
        session=session,
        emit=emit,
        tier=tier,
    )
    # 真实链路里由 api/chat.py 收尾；测试里照做，否则 trace 缺 SUMMARY 行
    ctx.trace.close(cost_yuan=outcome.cost_yuan, total_ms=0, degraded=outcome.degraded)
    return RunResult(outcome=outcome, events=events, ctx=ctx, session=session)
