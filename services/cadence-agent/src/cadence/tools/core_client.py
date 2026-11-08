"""Core HTTP 客户端。

**代码里唯一允许发 HTTP 到 Core 的地方。**（反向验收第 1 条：
`grep -rn "httpx\\." src/ | grep -v core_client.py` 必须为空）

它做三件事：
1. 把 `{code,message,data}` 外壳拆开，只把 `data` 交给上层
2. 把 HTTP 状态 + 错误文案翻译成 Agent 的 14 个错误码
3. 对 5xx / 连接失败做指数退避重试（对 4xx 绝不重试）
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..config import Settings
from ..runtime.errors import ErrorCode, ToolError


class CoreClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._http = http
        self._sleep = sleep
        self._owns_http = http is None
        self.call_count = 0

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.settings.core_base_url.rstrip("/"),
                timeout=self._timeout(),
            )
        return self._http

    def _timeout(self) -> httpx.Timeout:
        """Core 的超时矩阵：connect 2s / read 3s（见 `Settings`，本文件不许写死数字）。

        确认流程里 `_assert_confirm_versions` 那次 GET 也走这里 —— 它是一次 Core 读，
        不是一次模型调用，绝不能借用 LLM 的 15s read 超时。
        """
        return httpx.Timeout(
            self.settings.core_timeout_s,
            connect=self.settings.core_connect_timeout_s,
        )

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        trace_id: str | None = None,
        actor: str = "agent",
        confirm_token: str | None = None,
        idempotency_key: str | None = None,
        tool_name: str | None = None,
    ) -> Any:
        headers: dict[str, str] = {"X-Actor": actor}
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        if confirm_token:
            headers["X-Confirm-Token"] = confirm_token
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key

        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        timeout = self._timeout()

        last_exc: Exception | None = None
        for attempt in range(self.settings.core_retry_times + 1):
            try:
                self.call_count += 1
                resp = await self._client().request(
                    method,
                    path,
                    params=clean_params or None,
                    json=body,
                    headers=headers,
                    # 逐请求再显式传一次：注入的 http client（测试/连接池）不会带我们的矩阵
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                last_exc = ToolError(
                    code=ErrorCode.UPSTREAM_UNAVAILABLE,
                    message=f"Core 请求超时（{method} {path}）: {exc}",
                    core_down=True,
                )
            except httpx.HTTPError as exc:
                last_exc = ToolError(
                    code=ErrorCode.UPSTREAM_UNAVAILABLE,
                    message=f"Core 连接失败（{method} {path}）: {exc}",
                    core_down=True,
                )
            else:
                # 5xx 才重试；4xx 是调用方的错，重试只会重复犯错
                if 500 <= resp.status_code < 600 and attempt < self.settings.core_retry_times:
                    last_exc = ToolError(
                        code=ErrorCode.UPSTREAM_UNAVAILABLE,
                        message=f"Core 返回 {resp.status_code}（{method} {path}）",
                        core_down=True,
                    )
                else:
                    return self._translate(resp, tool_name)

            assert last_exc is not None
            if attempt < self.settings.core_retry_times:
                await self._sleep(self.settings.core_retry_backoff_s * (2**attempt))
            else:
                raise last_exc

        raise last_exc  # pragma: no cover

    # ------------------------------------------------------------------

    def _translate(self, resp: httpx.Response, tool_name: str | None) -> Any:
        """状态码 → 错误码；成功则返回 `data`。"""
        status = resp.status_code
        if status == 204 or not resp.content:
            if resp.is_success:
                return None
            raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, f"Core 返回空响应 {status}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ToolError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"Core 返回非 JSON（{status}）: {resp.text[:200]}",
            ) from exc

        code = payload.get("code") if isinstance(payload, dict) else None
        message = (payload.get("message") if isinstance(payload, dict) else None) or ""
        data = payload.get("data") if isinstance(payload, dict) else None

        if resp.is_success and code == 0:
            return data

        return self._error_for(status, message, data, tool_name)

    def _error_for(self, status: int, message: str, data: Any, tool_name: str | None) -> Any:
        if status == 403:
            # 删除的两阶段：失败响应里带下一步要用的令牌
            token = data.get("confirmToken") if isinstance(data, dict) else None
            snapshot = data.get("snapshot") if isinstance(data, dict) else None
            raise ToolError(
                ErrorCode.CONFIRM_REQUIRED,
                message or "该操作需要确认",
                details={"confirmToken": token, "snapshot": snapshot},
            )

        if status == 404:
            raise ToolError(
                ErrorCode.TASK_ID_NOT_FOUND,
                message or "任务不存在",
                suggestions=[{"tool": "task.search", "args": {}}],
            )

        if status == 409:
            raise ToolError(
                ErrorCode.CONFLICT,
                message or "版本冲突",
                suggestions=[{"tool": "task.search", "args": {"hint": "重新获取最新 version"}}],
            )

        if status == 422:
            code = classify_422(message, tool_name)
            raise ToolError(code, message or "业务规则不允许")

        if status == 400:
            raise ToolError(ErrorCode.VALIDATION_ERROR, message or "参数不合法")

        if status == 429:
            raise ToolError(ErrorCode.RATE_LIMITED, message or "请求过于频繁")

        if status >= 500:
            raise ToolError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                message or f"Core 服务异常（{status}）",
                core_down=True,
            )

        raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, f"未预期的状态 {status}: {message}")


# ---------------------------------------------------------------------------
# 422 的歧义：Core 只给数字 422 + 文案，不给符号码
# ---------------------------------------------------------------------------

_DEP_KEYWORDS = ("依赖", "循环", "前置", "环", "depends")


def classify_422(message: str, tool_name: str | None = None) -> ErrorCode:
    """区分 422 的两种含义。

    ⚠️ 契约缺陷：Core 的 422 同时用于 DEPENDENCY_VIOLATION 和 ILLEGAL_TRANSITION，
    响应体里只有 `{code: 422, message}`，没有符号码，只能靠文案 + 调用上下文推断。
    已在 docs/接口文档.md 记录为待 SP1 v0.3 补的字段（errorCode）。

    兜底优先级：文案关键词 > 工具语义 > ILLEGAL_TRANSITION
    """
    text = message or ""
    if any(k in text for k in _DEP_KEYWORDS):
        return ErrorCode.DEPENDENCY_VIOLATION
    if tool_name == "task.complete":
        # 完成操作返回 422，最可能是"前置依赖未完成"
        return ErrorCode.DEPENDENCY_VIOLATION
    return ErrorCode.ILLEGAL_TRANSITION
