"""工具基类与返回结构。

一个工具只做一件事（P5）：`task.create` 不负责建依赖，`task.split` 不负责排期。
组合由 Agent 的循环完成 —— **这正是 Agent 存在的意义**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pydantic import BaseModel

from ..guard.permissions import Permission
from ..runtime.errors import ToolError

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import TurnContext


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: ToolError | None = None
    summary: str = ""
    duration_ms: int = 0
    #: 模型实际看到的内容（截断 + DATA 包裹后）
    model_payload: Any = None
    #: 是否产生了写操作（用于 actions_taken 与审计）
    mutated: bool = False

    def to_model_json(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "data": self.model_payload}
        assert self.error is not None
        return self.error.to_tool_json()


ToolHandler = Callable[[Any, "TurnContext"], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    handler: ToolHandler
    args_model: type[BaseModel] | None = None
    permission: Permission = Permission.READ
    #: 影响 >1 条数据时要先 dry_run，再 apply（P6）
    supports_dry_run: bool = False
    #: 批量阈值：超过则必须确认
    batch_threshold: int | None = None
    #: 返回值是否需要截断（列表类必须）
    truncate: bool = True
    #: 是否暴露给模型。未实现的桩工具不要暴露 ——
    #: 模型看到就会去用它，然后拿到"未实现"，白白浪费一轮和一份 schema token。
    exposed: bool = True
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.batch_threshold is None:
            self.batch_threshold = 3


class Timer:
    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)
