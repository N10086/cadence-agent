"""会话状态。

结构刻意做小（PRD 02 §5.2）：

    会话状态 = 摘要 + 最近 3 轮 + 待确认操作 + 已确认约束

**永远不存任务列表。** 需要时用 `task.search` 查。
这条破了，上下文中期必然爆炸，而你会以为是模型不行。

存储：进程内 LRU + JSON 文件，**不引 Redis**（ADR D8 / 风险表"30 行代码能解决的别上组件"）。
并发：**会话级串行化**（进程内 asyncio.Lock）。不加的话两个请求同时读改写会互相覆盖，
表现为"AI 忘了刚才说过什么"——这个 bug 极难复现，必须一开始就有。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..llm.cost import estimate_tokens
from .errors import ErrorCode, ToolError


@dataclass
class Turn:
    role: str  # user / assistant
    content: str
    at: float = field(default_factory=time.time)


@dataclass
class SessionState:
    session_id: str
    summary: str = ""
    recent_turns: list[Turn] = field(default_factory=list)
    pending_confirm: dict[str, Any] | None = None
    constraints: dict[str, Any] = field(default_factory=dict)
    last_trace_id: str = ""
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    def add_turn(self, role: str, content: str, *, keep: int = 3) -> None:
        self.recent_turns.append(Turn(role=role, content=content))
        if len(self.recent_turns) > keep:
            self.recent_turns = self.recent_turns[-keep:]
        self.touch()

    def remember_constraint(self, key: str, value: Any) -> None:
        self.constraints[key] = value
        self.touch()

    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "recent_turns": [asdict(t) for t in self.recent_turns],
            "pending_confirm": self.pending_confirm,
            "constraints": self.constraints,
            "last_trace_id": self.last_trace_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        return cls(
            session_id=data["session_id"],
            summary=data.get("summary", ""),
            recent_turns=[Turn(**t) for t in data.get("recent_turns", [])],
            pending_confirm=data.get("pending_confirm"),
            constraints=data.get("constraints", {}),
            last_trace_id=data.get("last_trace_id", ""),
            updated_at=data.get("updated_at", time.time()),
        )


class SessionStore:
    def __init__(
        self,
        directory: Path,
        *,
        ttl_s: int = 1800,
        max_cached: int = 200,
        recent_turns: int = 3,
        clock=time.time,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_s
        self.max_cached = max_cached
        self.recent_turns = recent_turns
        self._clock = clock
        self._cache: dict[str, SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- 串行化 -----------------------------------------------------------

    def lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def acquire(self, session_id: str, *, timeout_s: float) -> asyncio.Lock:
        """拿会话锁，最多等 `timeout_s`；超时抛 `ToolError(SESSION_BUSY)`（HTTP 409）。

        为什么要有上限：D2 的默认是"同会话第二个请求排队"，但没有上限的排队等于把
        用户挂死。20s 是"上一条真的卡住了"的判定线，宁可让第二条带 409 去重试。

        为什么超时是 409 而不是 503：503 表示整机繁忙（准入队列满），
        409 表示**这个会话**忙 —— 换一个会话完全不受影响，客户端不该全局退避。
        """
        lock = self.lock(session_id)
        try:
            async with asyncio.timeout(timeout_s):
                await lock.acquire()
        except TimeoutError as exc:
            raise ToolError(
                ErrorCode.SESSION_BUSY,
                f"会话 {session_id} 上一条消息还在处理（等锁超过 {timeout_s:.0f}s），请稍后再发",
                suggestions=[{"hint": "等这条处理完再发，或换一个会话", "reason": "会话级串行化"}],
            ) from exc
        return lock

    def locked_count(self) -> int:
        """当前被持有的会话锁数量。压测用它断言"锁泄漏=0"。"""
        return sum(1 for lock in self._locks.values() if lock.locked())

    # ---- 读写 -------------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_") or "invalid"
        return self.directory / f"{safe}.json"

    def get(self, session_id: str) -> SessionState:
        cached = self._cache.get(session_id)
        if cached is not None and not self._expired(cached):
            return cached

        path = self._path(session_id)
        if path.exists():
            try:
                state = SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if not self._expired(state):
                    self._cache[session_id] = state
                    return state
            except (json.JSONDecodeError, KeyError):
                pass

        state = SessionState(session_id=session_id)
        self._cache[session_id] = state
        return state

    def save(self, state: SessionState) -> None:
        state.touch()
        path = self._path(state.session_id)
        path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        self._cache[state.session_id] = state
        self._evict()

    def _evict(self) -> None:
        while len(self._cache) > self.max_cached:
            oldest = min(self._cache.values(), key=lambda s: s.updated_at)
            self._cache.pop(oldest.session_id, None)
            self._locks.pop(oldest.session_id, None)

    def _expired(self, state: SessionState) -> bool:
        return (self._clock() - state.updated_at) > self.ttl_s

    def sweep(self) -> int:
        now = self._clock()
        dead = [sid for sid, s in self._cache.items() if (now - s.updated_at) > self.ttl_s]
        for sid in dead:
            self._cache.pop(sid, None)
            self._locks.pop(sid, None)
        removed = 0
        for path in self.directory.glob("*.json"):
            try:
                if (now - path.stat().st_mtime) > self.ttl_s:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return len(dead) + removed

    # ---- 上下文压缩 -------------------------------------------------------

    def needs_compression(self, messages: list[dict[str, Any]], limit: int) -> bool:
        return estimate_messages(messages) > limit

    @staticmethod
    def estimate(messages: list[dict[str, Any]]) -> int:
        return estimate_messages(messages)

    def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_last: int = 6,
        max_chars: int = 240,
    ) -> list[dict[str, Any]]:
        return compress_messages(messages, keep_last=keep_last, max_chars=max_chars)


# ---------------------------------------------------------------------------
# 模块级函数：循环直接用，不必持有 SessionStore
# ---------------------------------------------------------------------------


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        if m.get("tool_calls"):
            total += estimate_tokens(json.dumps(m["tool_calls"], ensure_ascii=False))
    return total


def compress_messages(
    messages: list[dict[str, Any]],
    *,
    keep_last: int = 6,
    max_chars: int = 240,
) -> list[dict[str, Any]]:
    """压缩历史：保留 system、最近若干条，并把较早的 tool 消息压成首行摘要。

    刻意不调用模型做摘要——那会再花一次钱，且引入不确定性；
    等 SP3 有了快模型再换成真摘要（届时 `date_parse_accuracy` 那类指标才有意义）。
    """
    if len(messages) <= keep_last + 1:
        return messages

    head = messages[0] if messages and messages[0].get("role") == "system" else None
    rest = messages[1:] if head else messages
    cut = max(0, len(rest) - keep_last)

    compressed: list[dict[str, Any]] = []
    if head:
        compressed.append(head)

    for index, msg in enumerate(rest):
        if index >= cut:
            compressed.append(msg)
            continue
        if msg.get("role") == "tool":
            text = str(msg.get("content") or "")
            first_line = text.split("\n", 1)[0][:max_chars]
            compressed.append({**msg, "content": f"[已压缩] {first_line}"})
        else:
            compressed.append(msg)

    return compressed
