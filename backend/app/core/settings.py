"""唯一根配置模块（T024）。

本项目所有环境配置只在此处装配（禁止并行创建 core/config.py 等第二配置真相源）。
- 凭证字段统一使用 :class:`pydantic.SecretStr`，避免进入日志；
- 各部分配置按契约前缀装配：``DATABASE_``/``AUTH_``/``RATE_LIMIT_``/``DOCUMENT_``/
  ``MODEL_GATEWAY_``/``RETRIEVAL_``/``MESSAGE_``；
- 环境变量文件按 ``APP_ENV`` 选择：本地开发加载 ``.env.local``，自动化测试加载
  ``.env.test``，云端 staging/production 不读取仓库中的任何 ``.env`` 文件，由
  Docker/CI 直接注入环境变量；
- 缺少关键变量时应用启动直接失败（``app.core.readiness.assert_startup_config``）；
- ``APP_ENV=production``（含 staging）拒绝使用默认数据库密码、开发密钥与本地存储
  路径回退（要求关键变量显式注入）。
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.infrastructure.model_gateway.config import ModelGatewaySettings
from app.infrastructure.rate_limit.config import RateLimitSettings
from app.infrastructure.storage.config import StorageSettings
from app.services.retrieval_config import RetrievalSettings

AppEnv = Literal["development", "test", "staging", "production"]

# 环境模式 → 读取的环境变量文件；staging/production 不读仓库内文件。
_ENV_FILE_BY_MODE: dict[str, str | None] = {
    "development": ".env.local",
    "test": ".env.test",
    "staging": None,
    "production": None,
}

# 本地开发默认数据库连接串（含默认密码 orionamesh:orionamesh）。
_DEFAULT_DATABASE_URL = "postgresql+psycopg://orionamesh:orionamesh@localhost:5432/orionamesh"

# 部署环境（staging/production）：必须显式注入的关键变量。
_DEPLOYMENT_REQUIRED_ENV_VARS = (
    "DATABASE_URL",
    "REDIS_URL",
    "DOCUMENT_STORAGE_ROOT",
    "AUTH_JWT_SECRET_KEY",
)


def _resolve_env_file() -> str | None:
    mode = os.environ.get("APP_ENV", "development")
    return _ENV_FILE_BY_MODE.get(mode, ".env.local")


class Settings(BaseSettings):
    """应用配置（唯一真相源）。"""

    model_config = SettingsConfigDict(env_prefix="", env_file=_resolve_env_file(), extra="ignore")

    app_name: str = "orionamesh-api"
    app_version: str = "0.1.0"

    # 环境模式：决定环境变量文件加载与部署安全校验（见模块 docstring）。
    app_env: AppEnv = "development"

    # 数据库连接（Alembic 与 SQLAlchemy 会话共用）；本地开发默认值便于启动，
    # 部署环境必须显式注入（见 assert_startup_config）。
    database_url: SecretStr = SecretStr(_DEFAULT_DATABASE_URL)
    # 必填 HS256 Access Token 签名密钥；UTF-8 编码后至少 32 字节，且不得与限流/供应商凭证复用。
    auth_jwt_secret_key: SecretStr = SecretStr("")
    # Redis 连接串；限流计数与 Celery 队列共用（本地默认值便于启动）。
    redis_url: str = "redis://localhost:6379/0"

    # 分级限流（RATE_LIMIT_*）；default_factory 保证每次构造时重新读取环境变量。
    # 嵌套 BaseSettings 不继承父级 env_file，必须显式传入同一环境文件，否则
    # .env.local/.env.test 中的 RATE_LIMIT_* 等值被静默忽略（回归测试锁定）。
    # pyright 无法解析 pydantic-settings 以 __pydantic_self__ 开头的 __init__
    # 签名（运行时签名声明了 _env_file），故对调用点加定向 ignore。
    rate_limit: RateLimitSettings = Field(
        default_factory=lambda: RateLimitSettings(
            _env_file=_resolve_env_file(),  # type: ignore[reportCallIssue]
        )
    )
    # 资料持久卷与处理（DOCUMENT_*）
    storage: StorageSettings = Field(
        default_factory=lambda: StorageSettings(
            _env_file=_resolve_env_file(),  # type: ignore[reportCallIssue]
        )
    )
    # 模型出口网关（MODEL_GATEWAY_*）
    model_gateway: ModelGatewaySettings = Field(
        default_factory=lambda: ModelGatewaySettings(
            _env_file=_resolve_env_file(),  # type: ignore[reportCallIssue]
        )
    )
    # 检索与消息恢复（RETRIEVAL_* / MESSAGE_STREAMING_STALE_SECONDS）
    retrieval: RetrievalSettings = Field(
        default_factory=lambda: RetrievalSettings(
            _env_file=_resolve_env_file(),  # type: ignore[reportCallIssue]
        )
    )

    @property
    def is_deployment(self) -> bool:
        """staging/production 视为部署环境：不读取仓库环境文件并要求显式注入。"""
        return self.app_env in ("staging", "production")

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.app_env not in _ENV_FILE_BY_MODE:
            raise ValueError(
                f"APP_ENV must be one of {sorted(_ENV_FILE_BY_MODE)}, got {self.app_env!r}"
            )
        self._isolate_secrets()
        return self

    def _isolate_secrets(self) -> None:
        jwt_key = self.auth_jwt_secret_key.get_secret_value()
        if jwt_key and jwt_key == self.rate_limit.subject_hmac_key.get_secret_value():
            raise ValueError(
                "AUTH_JWT_SECRET_KEY must not be reused as RATE_LIMIT_SUBJECT_HMAC_KEY"
            )
        if jwt_key and jwt_key == self.model_gateway.api_key.get_secret_value():
            raise ValueError("AUTH_JWT_SECRET_KEY must not be reused as MODEL_GATEWAY_API_KEY")
        if (
            self.rate_limit.subject_hmac_key.get_secret_value()
            and self.rate_limit.subject_hmac_key.get_secret_value()
            == self.model_gateway.api_key.get_secret_value()
        ):
            raise ValueError(
                "RATE_LIMIT_SUBJECT_HMAC_KEY must not be reused as MODEL_GATEWAY_API_KEY"
            )

    @property
    def database_url_value(self) -> str:
        """明文连接串（仅供引擎与迁移使用；不得写入日志）。"""
        return self.database_url.get_secret_value()

    @property
    def auth_jwt_secret_key_value(self) -> str:
        return self.auth_jwt_secret_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    """返回缓存的全局设置实例；测试可通过覆盖环境变量后调用。"""
    return Settings()


def deployment_required_env_vars() -> tuple[str, ...]:
    """部署环境必须显式注入的关键变量（用于启动校验提示）。"""
    return _DEPLOYMENT_REQUIRED_ENV_VARS
