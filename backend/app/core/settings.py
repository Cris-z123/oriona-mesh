"""唯一根配置模块。

本项目所有环境配置只在此处装配（禁止并行创建 core/config.py 等第二配置真相源）。
Phase 1 只提供最小可运行配置；Phase 2（T024/T026）在此基础上补齐认证密钥、限流、
存储与模型网关的必填配置就绪校验。不引入环境模式变量。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。

    环境变量无前缀（如 ``DATABASE_URL``）；``.env`` 文件可选，便于本地开发。
    """

    app_name: str = "orionamesh-api"
    app_version: str = "0.1.0"

    # 数据库连接；Alembic 与 SQLAlchemy 会话均从这里读取。
    # 本地开发默认值便于启动，部署环境必须通过 DATABASE_URL 覆盖。
    # 含数据库凭证；Phase 2（T024）装配统一就绪校验时改用 SecretStr，避免凭证进入日志。
    database_url: str = "postgresql+psycopg://orionamesh:orionamesh@localhost:5432/orionamesh"

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """返回缓存的全局设置实例；测试可通过覆盖环境变量后调用。"""
    return Settings()
