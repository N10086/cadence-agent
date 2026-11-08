"""OpenAI-compatible 客户端（DeepSeek / 智谱共用 90% 代码）。

两个 provider 的差异只有四处：超时、是否支持 reasoning_content、模型名、定价。
所以这里一个基类 + 两个薄子类，而不是把 HTTP 写两遍。

统一错误：上层只捕 `llm.base` 里的异常，永远看不到 httpx 的类型。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .base import (
    LLMBadOutput,
    LLMBadRequest,
    LLMError,
    LLMEvent,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    LLMUnavailable,
    ToolCall,
)
from .timeouts import DEFAULT_LLM_CONNECT_TIMEOUT_S, DEFAULT_LLM_READ_TIMEOUT_S


class OpenAICompatibleClient:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float = DEFAULT_LLM_READ_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_LLM_CONNECT_TIMEOUT_S,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self._client = http
        self._owns_http = http is None

    # ---- 生命周期 ---------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_s, connect=self.connect_timeout_s),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_http and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        """每次请求都显式带 Authorization。

        不依赖 client 的默认头：注入自定义 http client（测试、或未来换连接池）时，
        默认头不会被带过去 —— 那是很难查的一类 bug。
        """
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    # ---- 错误映射 ---------------------------------------------------------

    @staticmethod
    def _raise_for_status(resp: httpx.Response, body: str) -> None:
        if resp.is_success:
            return
        code = resp.status_code
        if code == 429:
            retry_after = resp.headers.get("Retry-After")
            raise LLMRateLimited(
                f"provider 限流 429：{body[:200]}",
                retry_after_s=float(retry_after) if retry_after else None,
            )
        if code >= 500:
            raise LLMUnavailable(f"provider {code}：{body[:200]}", raw=body)
        if code in (401, 403):
            raise LLMBadRequest(f"provider 鉴权失败 {code}：{body[:200]}", raw=body)
        raise LLMBadRequest(f"provider {code}：{body[:200]}", raw=body)

    @staticmethod
    def _wrap_transport(exc: Exception) -> LLMError:
        if isinstance(exc, httpx.TimeoutException):
            return LLMTimeout(f"provider 超时：{exc}")
        if isinstance(exc, httpx.HTTPError):
            return LLMUnavailable(f"provider 连接失败：{exc}")
        raise exc

    # ---- 请求体 -----------------------------------------------------------

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ---- 非流式 -----------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload = self._payload(messages, tools, temperature=temperature, max_tokens=max_tokens, stream=False)
        try:
            resp = await self._http().post("/chat/completions", json=payload, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_transport(exc) from exc

        body = resp.text
        self._raise_for_status(resp, body)

        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMBadOutput(f"provider 返回结构不符：{body[:300]}", raw=body) from exc

        usage = data.get("usage") or {}
        tool_calls = self._parse_tool_calls(message.get("tool_calls") or [])

        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            model=data.get("model") or self.model,
            finish_reason=choice.get("finish_reason") or "stop",
            reasoning=message.get("reasoning_content"),
        )

    # ---- 流式 -------------------------------------------------------------

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        payload = self._payload(messages, tools, temperature=temperature, max_tokens=max_tokens, stream=True)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        model = self.model
        finish_reason = "stop"

        try:
            async with self._http().stream(
                "POST", "/chat/completions", json=payload, headers=self._headers()
            ) as resp:
                if not resp.is_success:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    self._raise_for_status(resp, body)

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue

                    model = data.get("model") or model
                    if data.get("usage"):
                        usage = data["usage"]
                    choices = data.get("choices") or []
                    if not choices:
                        continue

                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}

                    if delta.get("reasoning_content"):
                        reasoning_parts.append(delta["reasoning_content"])
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                        yield LLMEvent(kind="token", text=delta["content"])

                    for tcd in delta.get("tool_calls") or []:
                        idx = int(tcd.get("index", 0))
                        slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tcd.get("id"):
                            slot["id"] = tcd["id"]
                        fn = tcd.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        except (LLMError, LLMBadOutput):
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_transport(exc) from exc

        tool_calls = self._parse_tool_calls(
            [
                {"id": s["id"], "function": {"name": s["name"], "arguments": s["arguments"]}}
                for _, s in sorted(tool_acc.items())
            ]
        )

        if finish_reason != "stop" and not tool_calls and not text_parts:
            # 流被截断：半截的 tool_call 必须丢弃（比不执行危险得多）
            raise LLMBadOutput(f"流式响应不完整（finish_reason={finish_reason}）")

        yield LLMEvent(
            kind="done",
            response=LLMResponse(
                text="".join(text_parts),
                tool_calls=tool_calls,
                tokens_in=int(usage.get("prompt_tokens") or 0),
                tokens_out=int(usage.get("completion_tokens") or 0),
                model=model,
                finish_reason=finish_reason,
                reasoning="".join(reasoning_parts) or None,
            ),
        )

    # ---- 工具调用解析 -----------------------------------------------------

    @staticmethod
    def _parse_tool_calls(raw: list[dict[str, Any]]) -> list[ToolCall]:
        out: list[ToolCall] = []
        for i, item in enumerate(raw):
            fn = item.get("function") or {}
            name = fn.get("name") or ""
            if not name:
                continue
            args_raw = fn.get("arguments")
            if isinstance(args_raw, dict):
                args = args_raw
            else:
                text = (args_raw or "").strip() or "{}"
                try:
                    args = json.loads(text)
                except json.JSONDecodeError as exc:
                    # 失败矩阵 F1 的入口：模型输出非法 JSON
                    raise LLMBadOutput(f"工具参数不是合法 JSON：{text[:200]}", raw=text) from exc
                if not isinstance(args, dict):
                    raise LLMBadOutput(f"工具参数不是对象：{text[:200]}", raw=text)
            out.append(ToolCall(name=name, arguments=args, id=item.get("id") or f"call_{i}"))
        return out


class DeepSeekClient(OpenAICompatibleClient):
    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float = DEFAULT_LLM_READ_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_LLM_CONNECT_TIMEOUT_S,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        # 默认值来自 llm/timeouts.py（唯一来源）；生产装配由 config.llm_timeouts() 传入，
        # 这样"矩阵"和"客户端"不会各写一份数字然后漂移。
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
            http=http,
        )


class ZhipuClient(OpenAICompatibleClient):
    """GLM-4-Flash：不支持 reasoning_content，规划质量差。超时同样走矩阵。"""

    name = "zhipu"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float = DEFAULT_LLM_READ_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_LLM_CONNECT_TIMEOUT_S,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
            http=http,
        )
