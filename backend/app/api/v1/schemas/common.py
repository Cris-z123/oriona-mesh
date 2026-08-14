"""统一响应信封与响应工厂（决策 7）。

除 SSE 外，所有 API 响应均为 ``{code, data, msg, trace_id}`` JSON 信封：
- 成功 ``code=0``、``msg=""``；
- 业务错误 ``code`` 使用 OpenAPI ``ErrorCode`` 中定义的稳定错误码，HTTP 状态码表达传输语义；
- ``trace_id`` 取自请求中间件 ContextVar，非 SSE 响应必须携带。
"""

from typing import Any

from pydantic import BaseModel

from app.api.middleware.trace import current_trace_id

SUCCESS_CODE = 0
DEFAULT_ERROR_MSG = "系统繁忙，请稍后再试"
VALIDATION_ERROR_MSG = "请求参数不合法，请检查后重试"
RESOURCE_NOT_FOUND_MSG = "请求的资源不存在"
RATE_LIMIT_EXCEEDED_MSG = "请求过于频繁，请稍后再试"
PROTECTION_UNAVAILABLE_MSG = "系统繁忙，请稍后再试"
RESOURCE_CONFLICT_MSG = "请求与当前资源状态冲突"
TOKEN_INVALID_MSG = "请重新登录"
KNOWLEDGE_BASE_NOT_FOUND_MSG = "请求的知识库不存在"


class ApiEnvelope(BaseModel):
    """非 SSE 统一响应信封。"""

    code: int
    data: Any = None
    msg: str = ""
    trace_id: str


class ErrorEnvelope(ApiEnvelope):
    """业务错误信封：code 必须为非 0 稳定业务错误码。"""

    pass


def success_response(data: Any = None, msg: str = "") -> ApiEnvelope:
    """构造成功信封（code=0）。"""
    return ApiEnvelope(code=SUCCESS_CODE, data=data, msg=msg, trace_id=current_trace_id())


def error_response(code: int, msg: str, data: Any = None) -> ErrorEnvelope:
    """构造错误信封；调用方自行决定 HTTP 状态码。"""
    return ErrorEnvelope(code=code, data=data, msg=msg, trace_id=current_trace_id())
