"""FastAPI 应用入口。

启动流程：从唯一根配置模块加载设置 → 配置 structlog JSON 日志 → 注册 trace_id 中间件与
统一错误信封处理器。``/health`` 供容器健康检查使用（仅进程存活）；``/ready`` 执行
配置与运行时就绪检查（数据库扩展/Redis/持久卷），任一失败返回 503 与错误明细。
业务路由自 Phase 2 起注册到 ``/v1``。
"""

import logging

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.middleware.errors import register_exception_handlers
from app.api.middleware.rate_limit import RateLimitMiddleware
from app.api.middleware.trace import TraceMiddleware
from app.api.v1.router import api_router
from app.api.v1.schemas.common import error_response, success_response
from app.core.logging import configure_logging
from app.core.readiness import assert_startup_config, is_ready
from app.core.settings import get_settings

logger = structlog.get_logger()

configure_logging(logging.INFO)

try:
    settings = get_settings()
except Exception as exc:  # noqa: BLE001 - 配置构造失败（含非法值 ValidationError）统一启动失败
    raise SystemExit(f"startup configuration failed: {exc}") from exc
# 启动门禁：缺少关键变量或配置非法时直接失败退出（SystemExit），不进入服务循环。
assert_startup_config(settings)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="OrionaMesh 个人知识库 RAG MVP",
)

# 中间件顺序：RateLimit 先注册（内层），Trace 后注册（外层），保证限流响应与日志
# 携带 trace_id；限流拦截发生在路由/业务写入之前。
app.add_middleware(RateLimitMiddleware)
app.add_middleware(TraceMiddleware)
register_exception_handlers(app)
app.include_router(api_router)


@app.get("/health", tags=["infra"])
def health() -> dict:
    """健康检查：仅确认应用进程存活，不依赖数据库/Redis 就绪。"""
    logger.info("health_check")
    return success_response({"status": "ok"}).model_dump(mode="json")


@app.get("/ready", tags=["infra"])
def ready() -> JSONResponse:
    """就绪检查：配置必填项、数据库必需扩展、Redis 与本地持久卷。

    任一失败返回 503（非 SSE 统一信封，code=50001），错误明细仅列出检查项名称，
    不含凭证原值或内部路径细节之外的信息。
    """
    ok, errors = is_ready()
    if ok:
        return JSONResponse(
            status_code=200, content=success_response({"ready": True}).model_dump(mode="json")
        )
    logger.warning("not_ready", errors=errors)
    return JSONResponse(
        status_code=503,
        content=error_response(
            50001, "系统繁忙，请稍后再试", data={"ready": False, "errors": errors}
        ).model_dump(mode="json"),
    )


def main() -> None:
    """开发命令入口：``uv run orionamesh-api``。"""
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
