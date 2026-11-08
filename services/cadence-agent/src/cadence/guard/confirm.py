"""确认令牌（Agent 侧，前缀 `cf_`）。

与 Core 的 `X-Confirm-Token`（前缀 `ct_`，SP1 实现）是两层不同的东西：

    Agent 的 cf_  ←→  前端 diff 弹窗「确认执行」
    Core 的 ct_   ←→  Agent 真正调 DELETE 时 Core 要求的令牌

为什么两层：Agent 要的是「用户确认过这个方案」，
Core 要的是「调用方确认过要删这条数据」。两者生命周期和粒度都不同，
硬合成一层会让「用户确认了 6 条移动」被滥用成「随便删任何东西」。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingConfirm:
    token: str
    tool: str
    args: dict[str, Any]
    preview: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    ttl_s: int = 300
    used: bool = False
    #: 预览时各任务的 version，apply 时校验是否被别人改过（PLAN_STALE）
    versions: dict[str, int] = field(default_factory=dict)
    #: 用签发它的那个时钟判断过期，否则测试里冻结时间就失效了
    clock: Any = None

    def _now(self, now: float | None) -> float:
        if now is not None:
            return now
        return self.clock() if callable(self.clock) else time.time()

    def expired(self, now: float | None = None) -> bool:
        return (self._now(now) - self.created_at) > self.ttl_s

    def expires_in(self, now: float | None = None) -> int:
        return max(0, int(self.ttl_s - (self._now(now) - self.created_at)))

    def scope_mismatch(self, tool: str, args: dict[str, Any]) -> str | None:
        """检查「用户确认的方案」和「模型现在要执行的调用」是不是同一件事。

        令牌只绑工具名是不够的：用户确认删除 A，模型可以拿同一个令牌删除 B。
        这里先比 tool，再比 task_id；split 没有单个 task_id，就比子任务序列。
        返回 None 表示可以继续，返回字符串表示应当按 PLAN_STALE 拒绝。
        """
        if self.tool != tool:
            return f"确认令牌属于 {self.tool}，当前调用的是 {tool}"

        expected_task = self.args.get("task_id")
        if expected_task is not None:
            actual_task = args.get("task_id")
            if actual_task != expected_task:
                return f"确认令牌绑定任务 {expected_task}，当前调用的是 {actual_task}"

        expected_children = self.args.get("children")
        if isinstance(expected_children, list):
            actual_children = args.get("children")
            if not isinstance(actual_children, list):
                return "确认令牌绑定的是拆解子任务，当前调用缺少 children"
            expected_sig = [
                (child.get("title"), child.get("estimate_min"))
                for child in expected_children
                if isinstance(child, dict)
            ]
            actual_sig = [
                (child.get("title"), child.get("estimate_min"))
                for child in actual_children
                if isinstance(child, dict)
            ]
            if expected_sig != actual_sig:
                return "确认令牌绑定的子任务与当前调用不一致，请重新预览"

        return None


class ConfirmStore:
    """单次有效 + 5 分钟过期。

    `take()` 是**消费式**的：取出来就标记已用。
    这样「用户连点两次确认」不会执行两遍。
    """

    def __init__(self, ttl_s: int = 300, *, clock=time.time) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._items: dict[str, PendingConfirm] = {}

    def issue(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        preview: dict[str, Any] | None = None,
        versions: dict[str, int] | None = None,
    ) -> PendingConfirm:
        pending = PendingConfirm(
            token="cf_" + secrets.token_hex(8),
            tool=tool,
            args=dict(args),
            preview=preview or {},
            created_at=self._clock(),
            ttl_s=self.ttl_s,
            versions=versions or {},
            clock=self._clock,
        )
        self._items[pending.token] = pending
        return pending

    def peek(self, token: str) -> PendingConfirm | None:
        pending = self._items.get(token)
        if pending is None:
            return None
        if pending.used or pending.expired(self._clock()):
            return None
        return pending

    def take(self, token: str) -> PendingConfirm | None:
        pending = self.peek(token)
        if pending is None:
            return None
        pending.used = True
        self._items.pop(token, None)
        return pending

    def discard(self, token: str) -> bool:
        return self._items.pop(token, None) is not None

    def sweep(self) -> int:
        now = self._clock()
        dead = [t for t, p in self._items.items() if p.expired(now)]
        for t in dead:
            self._items.pop(t, None)
        return len(dead)

    def __len__(self) -> int:
        return len(self._items)
