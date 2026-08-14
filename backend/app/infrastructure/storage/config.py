"""资料持久卷/解析/并发/上传幂等配置（quickstart 资料处理配置契约）。

字段名对应 ``DOCUMENT_*`` 环境变量；本地持久卷根目录默认容器内
``/data/orionamesh``，数据库只保存相对对象键。
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSettings(BaseSettings):
    """本地持久卷、解析限制、处理名额与上传幂等配置。"""

    model_config = SettingsConfigDict(env_prefix="DOCUMENT_", extra="ignore")

    storage_root: str = Field(default="/data/orionamesh", min_length=1)
    processing_max_per_user: int = Field(default=3, ge=1)
    processing_lease_seconds: int = Field(default=300, ge=1)
    upload_pending_timeout_seconds: int = Field(default=300, ge=1)
    parse_timeout_seconds: int = Field(default=60, ge=1)
    parse_max_expanded_bytes: int = Field(default=209_715_200, ge=1)
    upload_idempotency_ttl_seconds: int = Field(default=86_400, ge=1)
