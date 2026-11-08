"""可编排的假模型。

这不是"测试时才用的玩具"——它是**一等公民**：
没 key 时开发、9 类失败复现、SP4 评测的确定性回放，都靠它。

用法：

    fake = FakeLLM([
        call("task.search", keyword="牛奶"),
        call("task.complete", task_id="t_001", version=2),
        say("已把「买牛奶」标记完成。"),
    ])

每个元素可以是一次"模型回合"：说话、调工具、抛错、慢响应。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .base import LLMBadOutput, LLMEvent, LLMError, LLMResponse, LLMTimeout, ToolCall


@dataclass
class FakeStep:
    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_exc: Exception | None = None
    delay_s: float = 0.0
    tokens_in: int = 100
    tokens_out: int = 20
    raw_arguments: str | None = None  # 模拟"模型输出非法 JSON"


# ---- 便利构造器（让剧本读起来像剧本） -------------------------------------


def say(text: str, *, tokens_out: int = 20) -> dict[str, Any]:
    return {"text": text, "tokens_out": tokens_out}


def call(name: str, /, **arguments: Any) -> dict[str, Any]:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def calls(*items: dict[str, Any]) -> dict[str, Any]:
    return {"tool_calls": list(items)}


def tc(name: str, /, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def fail(exc: Exception | str) -> dict[str, Any]:
    if isinstance(exc, str):
        exc = LLMTimeout(exc) if exc == "timeout" else LLMError(exc)
    return {"raise": exc}


def slow(seconds: float, *, text: str | None = None) -> dict[str, Any]:
    return {"delay_s": seconds, "text": text}


def bad_json() -> dict[str, Any]:
    return {"raw_arguments": '{"keyword": "牛奶"'}


def _normalize(raw: Any) -> FakeStep:
    if isinstance(raw, FakeStep):
        return raw
    if isinstance(raw, str):
        return FakeStep(text=raw)
    if not isinstance(raw, dict):
        raise TypeError(f"剧本元素不认识：{raw!r}")
    return FakeStep(
        text=raw.get("text"),
        tool_calls=list(raw.get("tool_calls") or []),
        raise_exc=raw.get("raise"),
        delay_s=float(raw.get("delay_s") or 0.0),
        tokens_in=int(raw.get("tokens_in", 100)),
        tokens_out=int(raw.get("tokens_out", 20)),
        raw_arguments=raw.get("raw_arguments"),
    )


class FakeLLM:
    """按剧本逐个回合返回。剧本用尽后返回一句兜底文本（不会抛错）。"""

    name = "fake"

    def __init__(
        self,
        script: list[Any] | None = None,
        *,
        model: str = "fake-model",
        exhaustion_text: str = "（剧本已结束）",
    ) -> None:
        self._script = [_normalize(s) for s in (script or [])]
        self._cursor = 0
        self.model = model
        self.exhaustion_text = exhaustion_text
        self.calls: list[dict[str, Any]] = []  # 断言用：记录每次收到的 messages

    # ---- 供测试编排 -------------------------------------------------------

    def load(self, script: list[Any]) -> None:
        self._script = [_normalize(s) for s in script]
        self._cursor = 0

    @property
    def remaining(self) -> int:
        return max(0, len(self._script) - self._cursor)

    # ---- 内部 -------------------------------------------------------------

    def _next(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> FakeStep:
        self.calls.append({"messages": list(messages), "tools": list(tools or [])})
        if self._cursor >= len(self._script):
            return FakeStep(text=self.exhaustion_text)
        step = self._script[self._cursor]
        self._cursor += 1
        return step

    def _build(self, step: FakeStep) -> LLMResponse:
        if step.raise_exc is not None:
            raise step.raise_exc
        if step.raw_arguments is not None:
            raise LLMBadOutput("模型输出不是合法 JSON", raw=step.raw_arguments)

        tool_calls = [
            ToolCall(
                name=c["name"],
                arguments=dict(c.get("arguments") or {}),
                id=c.get("id") or f"call_{self._cursor}_{i}",
            )
            for i, c in enumerate(step.tool_calls)
        ]
        return LLMResponse(
            text=step.text or "",
            tool_calls=tool_calls,
            tokens_in=step.tokens_in,
            tokens_out=step.tokens_out,
            model=self.model,
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    # ---- LLMClient 协议 ---------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        step = self._next(messages, tools)
        if step.delay_s:
            await asyncio.sleep(step.delay_s)
        return self._build(step)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        step = self._next(messages, tools)
        if step.delay_s:
            await asyncio.sleep(step.delay_s)
        response = self._build(step)

        if response.text:
            # 按字符切片推送，模拟真实流式；工具调用回合不推 token
            for ch in _chunks(response.text, 8):
                yield LLMEvent(kind="token", text=ch)
                await asyncio.sleep(0)
        yield LLMEvent(kind="done", response=response)


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]
