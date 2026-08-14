"""就绪与启动配置校验（T016/T024/T026）。

- ``assert_startup_config``：缺少关键变量时应用启动直接失败（raise SystemExit，
  不进入服务循环）；部署环境（staging/production）额外要求关键变量显式注入，
  拒绝默认数据库密码/开发密钥/本地存储路径回退；
- ``validate_config`` 返回错误列表（/ready 与启动断言共用同一规则）；
- ``check_runtime`` 检查数据库扩展、Redis 与本地持久卷；
- 失败原因只包含变量名与规则描述，不包含凭证原值。
"""

import os
from pathlib import Path

import sqlalchemy
from sqlalchemy import text as sa_text
from sqlalchemy.exc import SQLAlchemyError

from app.core.settings import (
    Settings,
    deployment_required_env_vars,
    get_settings,
)
from app.infrastructure.model_gateway.config import ModelGatewaySettings

JWT_MIN_BYTES = 32

_STARTUP_FAILED_MSG = "startup configuration failed:"


def assert_startup_config(settings: Settings | None = None) -> None:
    """启动门禁：缺少关键变量或部署安全约束不满足时直接失败退出。

    :raises SystemExit: 配置不满足时列出全部错误并终止启动。
    """
    settings = settings or get_settings()
    errors = validate_config(settings)
    if errors:
        joined = "; ".join(errors)
        raise SystemExit(f"{_STARTUP_FAILED_MSG} {joined}")


def validate_config(settings: Settings | None = None) -> list[str]:
    """返回配置错误列表；空列表表示配置可报告就绪。"""
    settings = settings or get_settings()
    errors: list[str] = []

    # 认证：AUTH_JWT_SECRET_KEY 必填且 UTF-8 编码后至少 32 字节。
    jwt_key = settings.auth_jwt_secret_key_value
    if not jwt_key:
        errors.append("AUTH_JWT_SECRET_KEY is required")
    elif len(jwt_key.encode("utf-8")) < JWT_MIN_BYTES:
        errors.append("AUTH_JWT_SECRET_KEY must be at least 32 UTF-8 bytes")

    # 限流：主体摘要密钥必填。
    if not settings.rate_limit.subject_hmac_key.get_secret_value():
        errors.append("RATE_LIMIT_SUBJECT_HMAC_KEY is required")

    # 模型网关：provider、endpoint、凭证与必填模型。
    errors.extend(_validate_model_gateway(settings.model_gateway))

    # 检索：streaming 失联上限必须覆盖模型尝试预算加 60 秒。
    try:
        settings.retrieval.validate_streaming_stale(settings.model_gateway.attempt_budget_sum())
    except ValueError as exc:
        errors.append(str(exc))

    # 部署环境（staging/production）：关键变量必须由 Docker/CI 显式注入，
    # 拒绝回退到本地开发默认密码、开发密钥或本地存储路径。
    if settings.is_deployment:
        errors.extend(_validate_deployment_explicit_injection())

    return errors


def _validate_deployment_explicit_injection() -> list[str]:
    errors: list[str] = []
    for var in deployment_required_env_vars():
        if var not in os.environ:
            errors.append(
                f"{var} must be explicitly injected in deployment (APP_ENV production/staging); "
                "refusing to fall back to local development defaults"
            )
    return errors


def _validate_model_gateway(cfg: ModelGatewaySettings) -> list[str]:
    errors: list[str] = []
    if not cfg.endpoint:
        errors.append("MODEL_GATEWAY_ENDPOINT is required")
    if not cfg.api_key.get_secret_value():
        errors.append("MODEL_GATEWAY_API_KEY is required")
    if not cfg.query_rewrite_model:
        errors.append("MODEL_GATEWAY_QUERY_REWRITE_MODEL is required")
    if not cfg.generation_model:
        errors.append("MODEL_GATEWAY_GENERATION_MODEL is required")
    # rerank_model 为空表示禁用，不影响就绪。
    return errors


def check_runtime(
    settings: Settings | None = None, engine: sqlalchemy.Engine | None = None
) -> list[str]:
    """返回运行时错误列表：数据库必需扩展、Redis、本地持久卷可写。"""
    settings = settings or get_settings()
    errors: list[str] = []
    errors.extend(_check_db_extensions(engine))
    errors.extend(_check_redis())
    errors.extend(_check_storage_root(settings.storage.storage_root))
    return errors


def _check_db_extensions(engine: sqlalchemy.Engine | None) -> list[str]:
    errors: list[str] = []
    try:
        local_engine = engine or _session_engine()
        with local_engine.connect() as conn:
            available = {
                row[0]
                for row in conn.execute(sa_text("SELECT extname FROM pg_extension")).fetchall()
            }
        for required in ("vector", "pg_trgm"):
            if required not in available:
                errors.append(f"missing PostgreSQL extension: {required}")
    except SQLAlchemyError as exc:
        errors.append(f"database unreachable: {type(exc).__name__}")
    return errors


def _session_engine() -> sqlalchemy.Engine:
    from app.infrastructure.database.session import engine

    return engine


def _check_redis() -> list[str]:
    from app.core.redis import redis_healthy

    if not redis_healthy():
        return ["redis unreachable"]
    return []


def _check_storage_root(root: str) -> list[str]:
    try:
        path = Path(root)
        if not path.exists():
            return [f"storage root does not exist: {root}"]
        if not path.is_dir():
            return [f"storage root is not a directory: {root}"]
        probe = path / ".orionamesh-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return []
    except OSError as exc:
        return [f"storage root not writable: {root} ({type(exc).__name__})"]


def is_ready() -> tuple[bool, list[str]]:
    """合并配置与运行时检查；/ready 端点与测试共用。"""
    errors = validate_config() + check_runtime()
    return (not errors, errors)
