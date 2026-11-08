"""SP2 边界测试专用夹具。

与仓库根 `tests/conftest.py` 的区别：
1. **不使用 pytest 的 tmp_path**：当前执行环境的临时目录 ACL 有删除限制，
   pytest 在 session 收尾时清理 basetemp 会失败。这里用 `data/agent/_boundary/`
   下的唯一目录，测试之间互不干扰，也不依赖外部临时目录。
2. 固定时钟显式使用 +08:00 的 timezone，避免依赖 Windows 的 tzdata。
3. 提供 `run_script` / `make_ctx`，让 boundary 用例不必复制循环组装代码。
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

TZ = timezone(timedelta(hours=8))
FROZEN_NOW = datetime(2026, 11, 8, 10, 30, tzinfo=TZ)
_RUN_ROOT = Path(__file__).resolve().parents[4] / "data" / "agent" / "_boundary"


@pytest.fixture(scope="session", autouse=True)
def _prepare_run_root() -> None:
    # 尽力清理上一轮；失败也不影响本轮（每个用例的目录都是新的）
    shutil.rmtree(_RUN_ROOT, ignore_errors=True)
    _RUN_ROOT.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def workdir() -> Path:
    """每个用例一个独立目录，避免使用 pytest 的 tmp_path（当前环境清理会失败）。"""
    p = _RUN_ROOT / uuid.uuid4().hex
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def settings() -> Settings:
    return Settings(
        llm_mode="fake",
        deepseek_api_key="",
        zhipu_api_key="",
        data_dir=str(_RUN_ROOT / uuid.uuid4().hex),
        step_budget=8,
        token_budget_per_turn=200_000,
        max_context_tokens=4000,
        confirm_ttl_s=300,
        destructive_batch_threshold=3,
        llm_retry_times=1,
        llm_retry_backoff_s=0.0,
        core_retry_times=0,
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
            "version": 0,
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
    ctx: TurnContext | None = None,
) -> RunResult:
    assert isinstance(rt.llm, FakeLLM)
    rt.llm.load(script)
    ctx = ctx or make_ctx(rt, session_id=session_id)
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
    ctx.trace.close(cost_yuan=outcome.cost_yuan, total_ms=0, degraded=outcome.degraded)
    return RunResult(outcome=outcome, events=events, ctx=ctx, session=session)
