"""一轮对话的上下文。

一个 `TurnContext` 从 loop 入口传到工具、护栏、trace，
**所有跨层需要的东西都挂在这里**，避免到处传七八个参数。

其中 `visible_task_ids` 是白名单的唯一来源：只有这里能被追加，
且只在**成功的工具返回**之后追加。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..guard.confirm import ConfirmStore
from ..guard.validators import collect_ids, remember_ids
from ..tools.core_client import CoreClient
from .budget import Budget
from .trace import TraceRecorder


@dataclass
class TurnContext:
    settings: Settings
    core: CoreClient
    trace: TraceRecorder
    confirms: ConfirmStore
    trace_id: str
    session_id: str
    budget: Budget = field(default_factory=Budget)
    tier: str = "fast"
    prompt_version: str = ""
    step_index: int = 0
    degraded: bool = False
    degrade_reason: str = ""
    visible_task_ids: set[str] = field(default_factory=set)
    visible_versions: dict[str, int] = field(default_factory=dict)
    actions_taken: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    clarify_count: int = 0
    tool_calls: int = 0

    # ------------------------------------------------------------------

    def remember(self, payload: Any) -> set[str]:
        """把工具返回里的 id 加进白名单。"""
        added = remember_ids(self.visible_task_ids, payload)
        self._remember_versions(payload)
        return added

    def _remember_versions(self, payload: Any) -> None:
        if isinstance(payload, dict):
            tid = payload.get("id")
            ver = payload.get("version")
            if isinstance(tid, str) and isinstance(ver, int):
                self.visible_versions[tid] = ver
            for v in payload.values():
                self._remember_versions(v)
        elif isinstance(payload, (list, tuple)):
            for item in payload:
                self._remember_versions(item)

    def known_version(self, task_id: str) -> int | None:
        return self.visible_versions.get(task_id)

    def note_action(self, text: str) -> None:
        if text and text not in self.actions_taken:
            self.actions_taken.append(text)

    def idempotency_key(self, suffix: str = "") -> str:
        """P3：写操作必须幂等。键 = trace_id + 步号 + 后缀，重试安全。"""
        base = f"{self.trace_id}:{self.step_index}:{suffix}" if suffix else f"{self.trace_id}:{self.step_index}"
        return f"idem_{base}"

    def new_id(self, prefix: str = "x") -> str:
        return f"{prefix}_{secrets.token_hex(4)}"

    def referenced_ids(self, args: dict[str, Any]) -> list[str]:
        return collect_ids(args)

    def mark_degraded(self, reason: str) -> None:
        self.degraded = True
        self.degrade_reason = reason
