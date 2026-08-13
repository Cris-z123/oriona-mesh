"""trace_id 请求中间件。

每个请求生成或接收 UUID ``trace_id``（决策 7）：客户端可通过 ``X-Trace-Id`` 请求头透传，
非法值忽略并重新生成。trace_id 注入本模块的 ContextVar，供统一响应信封与 structlog 处理器
复用；响应头 ``X-Trace-Id`` 回写同一值，便于排障关联。实现为纯 ASGI 中间件，避免
BaseHTTPMiddleware 对流式响应（SSE）的缓冲影响。
"""

import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders

TRACE_HEADER = "X-Trace-Id"

# 单请求 trace_id 唯一来源；日志处理器与响应工厂均从这里读取。
TRACE_ID_VAR: ContextVar[str] = ContextVar("trace_id", default="")


def current_trace_id() -> str:
    """返回当前请求的 trace_id；无请求上下文时返回空串。"""
    return TRACE_ID_VAR.get()


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class TraceMiddleware:
    """为每个 HTTP 请求生成或透传 UUID trace_id。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        incoming = Headers(scope=scope).get(TRACE_HEADER.lower())
        if incoming is not None and _valid_uuid(incoming):
            # uuid.UUID 接受无连字符、大写、花括号等非规范形式；契约要求
            # format: uuid 的规范 8-4-4-4-12 形式，因此统一规范化后再使用。
            trace_id = str(uuid.UUID(incoming))
        else:
            trace_id = str(uuid.uuid4())

        # trace_id 同时写入 scope：未捕获异常会穿过本中间件展开（finally 先于
        # 外层 ServerErrorMiddleware 的 500 处理器执行），处理器需从 scope 恢复。
        scope["trace_id"] = trace_id
        token = TRACE_ID_VAR.set(trace_id)
        try:

            async def send_with_header(message):
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers[TRACE_HEADER] = trace_id
                await send(message)

            await self.app(scope, receive, send_with_header)
        finally:
            TRACE_ID_VAR.reset(token)
