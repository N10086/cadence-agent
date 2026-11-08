"""依赖装配。整个进程只有一份 Runtime。

这里故意不用 FastAPI 的 Depends 注入链：工具层需要的东西（core client、trace、
confirm store）是"每轮一个"的，塞进 DI 反而绕。一个 Runtime 单例 + 一个 per-turn
TurnContext，边界更清楚。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .config import Settings, get_settings
from .guard.confirm import ConfirmStore
from .llm.base import LLMClient
from .llm.fake import FakeLLM, call, say
from .llm.retry import Breaker
from .llm.router import Tier
from .runtime.errors import ErrorCode, ToolError
from .runtime.loop import Loop
from .runtime.session import SessionStore
from .runtime.trace import new_trace_id
from .api.sse import StreamStore
from .tools.core_client import CoreClient
from .tools.registry import build_default_registry


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        core: Any | None = None,
    ) -> None:
        self.settings = settings
        settings.ensure_dirs()

        self.registry = build_default_registry()
        self.core = core if core is not None else CoreClient(settings)
        self.llm = llm or build_llm(settings)
        self.breaker = Breaker(
            "llm",
            failure_threshold=settings.breaker_failure_threshold,
            window=settings.breaker_window,
            cooldown_s=settings.breaker_cooldown_s,
            cooldown_max_s=settings.breaker_cooldown_max_s,
        )
        self.sessions = SessionStore(
            settings.resolved_session_dir,
            ttl_s=settings.session_ttl_s,
            max_cached=settings.session_max_lru,
            recent_turns=settings.session_recent_turns,
        )
        self.confirms = ConfirmStore(settings.confirm_ttl_s)
        self.streams = StreamStore()
        self.loop = Loop(
            settings=settings,
            registry=self.registry,
            llm=self.llm,
            breaker=self.breaker,
        )
        self._background: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self.settings.resolved_llm_mode

    def new_trace_id(self) -> str:
        return new_trace_id()

    def track(self, task: asyncio.Task[Any]) -> None:
        """持有后台任务引用，避免被 GC；异常也不会静默。"""
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        for obj in (self.core, self.llm):
            closer = getattr(obj, "aclose", None)
            if closer is not None:
                await closer()

    # ------------------------------------------------------------------

    async def health(self) -> dict[str, str]:
        core_status = "DOWN"
        try:
            await asyncio.wait_for(
                self.core.request("GET", "/api/v1/tasks", params={"limit": 1}),
                timeout=self.settings.core_timeout_s,
            )
            core_status = "UP"
        except (ToolError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            core_status = f"DOWN: {type(exc).__name__}"

        status = "UP" if core_status == "UP" else "DOWN"
        return {"status": status, "core": core_status, "llmMode": self.mode}


def build_llm(settings: Settings) -> LLMClient:
    if settings.resolved_llm_mode == "fake":
        return FakeLLM(default_script())

    from .llm.deepseek import DeepSeekClient

    if not settings.deepseek_api_key.strip():
        raise ToolError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "LLM_MODE=live 但没有配置 DEEPSEEK_API_KEY",
        )
    return DeepSeekClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_s=settings.deepseek_timeout_s,
    )


def default_script() -> list[Any]:
    """fake 模式的默认剧本：一条"查任务 → 汇报"的最短路径。

    只用于离线演示和冒烟；真正的测试各自喂自己的剧本。
    """
    return [
        call("task.search", limit=10),
        say("我查到了你当前的任务，详情见列表。需要我帮你标记完成其中某一项吗？"),
    ]


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime(get_settings())
    return _runtime


def set_runtime(runtime: Runtime | None) -> None:
    """测试用：注入自己的 Runtime（通常带 FakeLLM）。"""
    global _runtime
    _runtime = runtime


def router_tier(text: str) -> Tier:
    from .llm.router import route

    return route(text).tier
