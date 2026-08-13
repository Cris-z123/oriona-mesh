"""统一异常映射与错误信封处理器（决策 7 / quickstart 后端优先验证 24）。

- 业务错误统一抛出 :class:`ApiError`，携带稳定业务错误码、固定提示与 HTTP 状态码；
- Pydantic 校验失败映射为 ``10003/400``；
- 未知路径等 Starlette 404 映射为 ``20007/404``，其余 HTTPException 保持状态码并使用
  ``50000`` 默认提示；
- 未捕获异常映射为 ``50000/500``，只记录脱敏后的结构化日志，不把异常或供应商响应返回给客户端。
"""

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.middleware.trace import TRACE_HEADER, TRACE_ID_VAR, current_trace_id
from app.api.v1.schemas.common import (
    DEFAULT_ERROR_MSG,
    RESOURCE_NOT_FOUND_MSG,
    VALIDATION_ERROR_MSG,
    error_response,
)

logger = structlog.get_logger()

_ERROR_CODE_UNKNOWN_PATH = 20007
_ERROR_CODE_INVALID_PARAMS = 10003
_ERROR_CODE_INTERNAL = 50000


class ApiError(Exception):
    """业务错误：稳定错误码 + 固定安全提示 + HTTP 状态码。"""

    def __init__(self, code: int, message: str, http_status: int = 400):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def register_exception_handlers(app: FastAPI) -> None:
    """注册统一错误处理器；所有非 SSE 分支均返回统一信封。"""

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=error_response(exc.code, exc.message).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_response(_ERROR_CODE_INVALID_PARAMS, VALIDATION_ERROR_MSG).model_dump(
                mode="json"
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 非 404 的 HTTPException（如 405/401 路由层错误）暂用 50000 默认提示；
        # 阶段 6 契约冻结（T084/T086）时须明确其业务码映射，避免语义漂移。
        if exc.status_code == 404:
            code, msg = _ERROR_CODE_UNKNOWN_PATH, RESOURCE_NOT_FOUND_MSG
        else:
            code, msg = _ERROR_CODE_INTERNAL, DEFAULT_ERROR_MSG
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response(code, msg).model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # 未捕获异常在 TraceMiddleware 的 finally 展开后才到达本处理器（ServerErrorMiddleware
        # 位于用户中间件外层），因此从 scope 恢复 trace_id，保证 500 信封与日志仍携带它。
        stored = request.scope.get("trace_id")
        if stored:
            TRACE_ID_VAR.set(stored)
        # 只记录脱敏元数据与异常类型；不得把异常详情、供应商响应或请求正文返回给客户端。
        logger.error(
            "unhandled_exception",
            method=request.method,
            path=request.url.path,
            exc_type=type(exc).__name__,
        )
        # ServerErrorMiddleware 通过原始 send 发送 500 响应，绕过了 TraceMiddleware 的
        # 响应头回写，因此在此显式回写 X-Trace-Id，保证客户端可凭响应头关联日志。
        return JSONResponse(
            status_code=500,
            headers={TRACE_HEADER: stored or current_trace_id()},
            content=error_response(_ERROR_CODE_INTERNAL, DEFAULT_ERROR_MSG).model_dump(mode="json"),
        )
