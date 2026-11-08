"""依赖装配。整个进程只有一份 Runtime。

这里故意不用 FastAPI 的 Depends 注入链：工具层需要的东西（core client、trace、
confirm store）是"每轮一个"的，塞进 DI 反而绕。一个 Runtime 单例 + 一个 per-turn
TurnContext，边界更清楚。

**L3 的资源图（只允许单向边）**

    会话锁（每 session 一把，唯一可等待锁）
        │
        ▼
    LLM 信号量（16，叶子节点；只在真正打 provider 的那一小段持有）

为什么这条注释必须留在这里：死锁的形式化理由就是"资源图无环"。任何新加的
`asyncio.Lock/Semaphore/Condition` 都要问一句：它会不会被某条路径反向拿？
准入队列（`runtime/queue.py`）刻意做成无锁，就是为了不往这张图里加边。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from .config import Settings, get_settings
from .guard.confirm import ConfirmStore
from .llm.base import LLMClient
from .llm.fake import FakeLLM, call, say
from .llm.retry import Breaker
from .llm.router import Tier
from .runtime.errors import ErrorCode, ToolError
from .runtime.loop import Loop
from .runtime.queue import TurnQueue
from .runtime.session import SessionStore
from .runtime.trace import new_trace_id
from .runtime.watchdog import watch_turn
from .api.sse import StreamStore
from .tools.core_client import CoreClient
from .tools.registry import build_default_registry

logger = logging.getLogger("cadence")


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
        # 双模型：reason 走 reasoning 模型（deepseek-flash），fast 走非 reasoning 别名
        # （deepseek-chat）。注入 llm 时（测试/降级）两层共用它，避免测试意外打真网。
        if llm is not None:
            self.llm = llm
            self.fast_llm = llm
        else:
            self.llm = build_llm(settings)
            self.fast_llm = build_fast_llm(settings)
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
        # L3-1：准入。无锁（见 runtime/queue.py 的说明），不会给资源图加边。
        self.queue = TurnQueue(settings.turn_queue_capacity)
        # L3-2：全局 LLM 信号量。叶子节点：只有 provider 调用点会拿它。
        self.llm_semaphore = asyncio.Semaphore(settings.llm_max_concurrency)
        # L4：评测冻结时钟。空值 = 真实时间；外部 live Agent 用
        # EVAL_FROZEN_NOW=2026-11-08T10:30:00+08:00 启动，题面里的"今天/本周三"才可复现。
        frozen_clock = None
        if settings.eval_frozen_now.strip():
            frozen_at = datetime.fromisoformat(settings.eval_frozen_now.replace("Z", "+00:00"))
            frozen_clock = lambda: frozen_at  # noqa: E731 - 纯值捕获，无状态
        self.loop = Loop(
            settings=settings,
            registry=self.registry,
            llm=self.llm,
            # 双模型：L1 降级换到 fast client（非 reasoning 别名），不只是换提示。
            fast_llm=self.fast_llm,
            breaker=self.breaker,
            llm_semaphore=self.llm_semaphore,
            clock=frozen_clock,
        )
        self._background: set[asyncio.Task[Any]] = set()
        #: trace_id → turn 任务。watchdog 靠它打印"该 trace 的协程栈"。
        self.turn_tasks: dict[str, asyncio.Task[Any]] = {}

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

    def watch(self, task: asyncio.Task[Any], trace_id: str) -> asyncio.Task[Any]:
        """给一个 turn 任务挂看门狗：到点只打栈，取消仍由 chat 的 asyncio.timeout 负责。"""
        self.turn_tasks[trace_id] = task
        task.add_done_callback(lambda _t: self.turn_tasks.pop(trace_id, None))
        watcher = asyncio.create_task(
            watch_turn(task, trace_id=trace_id, timeout_s=self.settings.turn_timeout_s, logger=logger),
            name=f"watchdog:{trace_id}",
        )
        self.track(watcher)
        return watcher

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self.turn_tasks.clear()
        for obj in (self.core, self.llm, self.fast_llm):
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


def _deepseek_client(settings: Settings, model: str) -> LLMClient:
    from .llm.deepseek import DeepSeekClient

    if not settings.deepseek_api_key.strip():
        raise ToolError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "LLM_MODE=live 但没有配置 DEEPSEEK_API_KEY",
        )
    connect_s, read_s = settings.llm_timeouts("deepseek")
    return DeepSeekClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=model,
        timeout_s=read_s,
        connect_timeout_s=connect_s,
    )


def build_llm(settings: Settings) -> LLMClient:
    """推理层客户端：会返回 reasoning_content（deepseek-flash）。"""
    if settings.resolved_llm_mode == "fake":
        return FakeLLM(default_script())
    return _deepseek_client(settings, settings.deepseek_model)


def build_fast_llm(settings: Settings) -> LLMClient:
    """快层客户端：非 reasoning 别名（deepseek-chat），更快更便宜。"""
    if settings.resolved_llm_mode == "fake":
        return FakeLLM(default_script())
    return _deepseek_client(settings, settings.deepseek_fast_model)


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
