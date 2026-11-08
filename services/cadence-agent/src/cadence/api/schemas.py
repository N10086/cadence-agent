"""HTTP 请求/响应模型。

字段名对外是 camelCase（对齐 docs/接口文档.md），对内是 snake_case。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId", min_length=1, max_length=100)
    message: str = Field(..., min_length=1, max_length=2000)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    confirm_token: str = Field(..., alias="confirmToken", min_length=1, max_length=100)
    # 破坏性操作必须由调用方显式表态：缺失时由全局 RequestValidationError 处理器返回 400，
    # 而不是默认 True 直接执行。false 仍然表示用户拒绝，走 discard 分支。
    approve: bool = Field(..., description="true 执行，false 放弃；不可省略")


class ChatStarted(BaseModel):
    streamId: str
    traceId: str
    degraded: bool = False


class ApiEnvelope(BaseModel):
    code: int = 0
    message: str = "success"
    data: Any = None


def ok(data: Any = None, *, code: int = 0, message: str = "success") -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}


def fail(code: int, message: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}
