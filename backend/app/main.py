"""FastAPI 应用入口。

启动流程：从唯一根配置模块加载设置 → 配置 structlog JSON 日志 → 注册 trace_id 中间件与
统一错误信封处理器。``/health`` 供容器健康检查使用，返回统一成功信封（非 SSE 响应契约）。
业务路由自 Phase 2 起注册到 ``/v1``；Phase 1 不包含任何用户故事实现。
"""

import logging

import structlog
from fastapi import FastAPI

from app.api.middleware.errors import register_exception_handlers
from app.api.middleware.trace import TraceMiddleware
from app.api.v1.schemas.common import success_response
from app.core.logging import configure_logging
from app.core.settings import get_settings

logger = structlog.get_logger()

configure_logging(logging.INFO)

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="OrionaMesh 个人知识库 RAG MVP",
)

app.add_middleware(TraceMiddleware)
register_exception_handlers(app)


@app.get("/health", tags=["infra"])
def health() -> dict:
    """健康检查：仅确认应用进程存活，不依赖数据库/Redis 就绪（就绪检查见 Phase 2）。"""
    logger.info("health_check")
    return success_response({"status": "ok"}).model_dump(mode="json")


def main() -> None:
    """开发命令入口：``uv run orionamesh-api``。"""
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
