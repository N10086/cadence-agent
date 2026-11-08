"""FastAPI 应用入口。

`uvicorn cadence.main:app --port 18000`
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin, chat
from .config import get_settings
from .deps import get_runtime, set_runtime
from .runtime.errors import ToolError

logger = logging.getLogger("cadence")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    )
    rt = get_runtime()
    logger.info(
        "cadence-agent 启动：mode=%s core=%s tools=%d traces=%s",
        rt.mode,
        settings.core_base_url,
        len(rt.registry),
        settings.resolved_trace_dir,
    )
    try:
        yield
    finally:
        await rt.aclose()
        set_runtime(None)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Cadence Agent",
        version="0.1.0",
        description="Agent 运行时：循环、工具、护栏、可观测（SP2）",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Last-Event-ID"],
    )

    app.include_router(chat.router)
    app.include_router(admin.router)

    @app.exception_handler(RequestValidationError)
    async def on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI 默认返回 {"detail": [...]}，会破坏 {code,message,data} 外壳。
        # 这里统一成 400 + 可读的一句话，前端和评测脚本都只认一种格式。
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", ()) if x != "body")
        message = f"参数 {loc} 不合法：{first.get('msg', '格式错误')}" if loc else "请求参数不合法"
        return JSONResponse(
            status_code=400,
            content={"code": 400, "message": message, "data": {"errors": errors}},
        )

    @app.exception_handler(ToolError)
    async def on_tool_error(request: Request, exc: ToolError) -> JSONResponse:
        status = _status_for(exc)
        return JSONResponse(
            status_code=status,
            content={"code": status, "message": exc.message, "data": {"errorCode": exc.code.value}},
        )

    @app.exception_handler(Exception)
    async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 反向验收：关掉 Core / 任何异常都不该返回 500 堆栈
        logger.exception("未预期异常: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": "服务内部错误", "data": None},
        )

    return app


def _status_for(exc: ToolError) -> int:
    from .runtime.errors import ErrorCode

    mapping = {
        ErrorCode.VALIDATION_ERROR: 400,
        ErrorCode.CONFIRM_REQUIRED: 403,
        ErrorCode.TASK_ID_NOT_FOUND: 404,
        ErrorCode.CONFLICT: 409,
        ErrorCode.PLAN_STALE: 409,
        ErrorCode.ILLEGAL_TRANSITION: 422,
        ErrorCode.DEPENDENCY_VIOLATION: 422,
        ErrorCode.HARD_CONSTRAINT_UNSATISFIABLE: 422,
        ErrorCode.NOT_IMPLEMENTED: 501,
        ErrorCode.RATE_LIMITED: 429,
        ErrorCode.TASK_ID_NOT_IN_CONTEXT: 422,
        ErrorCode.LOOP_DETECTED: 422,
        ErrorCode.BUDGET_EXCEEDED: 422,
        ErrorCode.UPSTREAM_UNAVAILABLE: 503,
        # L3：整机忙 → 503；单个会话忙 → 409；整轮超时 → 504
        ErrorCode.OVERLOADED: 503,
        ErrorCode.SESSION_BUSY: 409,
        ErrorCode.TURN_TIMEOUT: 504,
    }
    return mapping.get(exc.code, 500)


app = create_app()


def main() -> None:  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "cadence.main:app",
        host=settings.agent_host,
        port=settings.agent_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
