"""LLM 客户端协议与统一数据类型。

上层（runtime/loop.py）只认识这里的东西，不知道底下是智谱、DeepSeek 还是假模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """模型要求调用一次工具。"""

    name: str
    arguments: dict[str, Any]
    id: str = ""

    def fingerprint_payload(self) -> str:
        """用于去重：参数的键序不影响指纹。"""
        return json.dumps({"name": self.name, "args": self.arguments}, sort_keys=True, ensure_ascii=False)


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    finish_reason: str = "stop"
    reasoning: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


EventKind = Literal["token", "done"]


@dataclass
class LLMEvent:
    """流式事件：token 增量，或最终 done（带完整 LLMResponse）。"""

    kind: EventKind
    text: str = ""
    response: LLMResponse | None = None


@dataclass
class LLMUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cost_yuan: float = 0.0


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """所有 provider 错误的上游基类 —— 上层只捕这一个。"""

    provider: str = "unknown"
    retryable: bool = True

    def __init__(self, message: str, *, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class LLMTimeout(LLMError):
    retryable = True


class LLMRateLimited(LLMError):
    retryable = True

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LLMUnavailable(LLMError):
    retryable = True


class LLMBadRequest(LLMError):
    retryable = False


class LLMBadOutput(LLMError):
    """模型输出不是合法 JSON / 结构不符（失败矩阵 F1）。"""

    retryable = True


class LLMBreakerOpen(LLMError):
    """熔断器打开，本轮不打 provider。"""

    retryable = False


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------


class ToolSpec(dict):
    """OpenAI-compatible 工具定义（就是个 dict，加个类型别名便于阅读）。"""


@runtime_checkable
class LLMClient(Protocol):
    """两个实现：FakeLLM（可编排）与 DeepSeekClient（真模型）。"""

    name: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]: ...
