"""模型出口网关配置（quickstart 模型出口配置契约 / model-egress.md）。

字段名对应 ``MODEL_GATEWAY_*`` 环境变量；endpoint 无默认值且必须为合法 HTTPS
base URL，仅本地开发/自动化测试允许主机名精确为 ``localhost`` 或回环 IP 的 HTTP
endpoint；未知 provider、缺失/非法 endpoint、非法模型组合不得报告就绪。
"""

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 本机回环例外允许的主机名（精确匹配）。
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


class ModelGatewaySettings(BaseSettings):
    """供应商、端点、凭证与四类模型调用配置。"""

    model_config = SettingsConfigDict(env_prefix="MODEL_GATEWAY_", extra="ignore")

    provider: str = Field(default="openai-compatible", min_length=1)
    endpoint: str | None = None
    api_key: SecretStr = SecretStr("")
    sanitizer_policy_version: str = "v1"
    audit_payloads: bool = False

    embedding_model: str = "text-embedding-3-small"
    query_rewrite_model: str | None = None
    rerank_model: str | None = None
    generation_model: str | None = None

    embedding_timeout_seconds: int = Field(default=30, ge=1)
    embedding_max_retries: int = Field(default=2, ge=0)
    query_rewrite_timeout_seconds: int = Field(default=10, ge=1)
    query_rewrite_max_retries: int = Field(default=1, ge=0)
    rerank_timeout_seconds: int = Field(default=10, ge=1)
    rerank_max_retries: int = Field(default=1, ge=0)
    generation_first_token_timeout_seconds: int = Field(default=15, ge=1)
    generation_total_timeout_seconds: int = Field(default=120, ge=1)
    generation_max_retries: int = Field(default=1, ge=0)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        # 未知 provider 必须拒绝就绪（model-egress.md）；MVP 唯一承诺值。
        if value != "openai-compatible":
            raise ValueError(f"unknown model gateway provider: {value!r}")
        return value

    @field_validator("sanitizer_policy_version")
    @classmethod
    def _known_policy(cls, value: str) -> str:
        if value != "v1":
            raise ValueError(f"unknown sanitizer policy version: {value!r}")
        return value

    @field_validator("audit_payloads")
    @classmethod
    def _no_payload_audit(cls, value: bool) -> bool:
        # 必须保持 false；启动校验拒绝开启正文日志（FR-029）。
        if value:
            raise ValueError("MODEL_GATEWAY_AUDIT_PAYLOADS must remain false")
        return value

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"invalid model gateway endpoint: {value!r}")
        if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
            # 仅本地开发/自动化测试允许回环 HTTP；其他 HTTP endpoint 一律拒绝就绪。
            raise ValueError("HTTP endpoint is only allowed for localhost or loopback addresses")
        return value

    @property
    def reranker_enabled(self) -> bool:
        return bool(self.rerank_model)

    def attempt_budget_sum(self) -> int:
        """全部可配置改写/重排/生成调用的最大尝试预算之和（1 + 最大重试）。"""
        total = (1 + self.query_rewrite_max_retries) + (1 + self.generation_max_retries)
        if self.reranker_enabled:
            total += 1 + self.rerank_max_retries
        return total

    @model_validator(mode="after")
    def _validate_ranges(self) -> "ModelGatewaySettings":
        if self.embedding_timeout_seconds < 1 or self.query_rewrite_timeout_seconds < 1:
            raise ValueError("timeouts must be at least 1 second")
        if self.embedding_max_retries < 0 or self.query_rewrite_max_retries < 0:
            raise ValueError("max retries must be non-negative")
        return self

    def to_call_options(
        self, call_type: Literal["embedding", "query_rewrite", "rerank", "generation"]
    ) -> dict[str, Any]:
        """按调用类型返回超时/重试与模型选择（T034/T035 使用）。"""
        if call_type == "embedding":
            return {
                "model": self.embedding_model,
                "timeout_seconds": self.embedding_timeout_seconds,
                "max_retries": self.embedding_max_retries,
            }
        if call_type == "query_rewrite":
            return {
                "model": self.query_rewrite_model,
                "timeout_seconds": self.query_rewrite_timeout_seconds,
                "max_retries": self.query_rewrite_max_retries,
            }
        if call_type == "rerank":
            return {
                "model": self.rerank_model,
                "timeout_seconds": self.rerank_timeout_seconds,
                "max_retries": self.rerank_max_retries,
            }
        return {
            "model": self.generation_model,
            "first_token_timeout_seconds": self.generation_first_token_timeout_seconds,
            "total_timeout_seconds": self.generation_total_timeout_seconds,
            "max_retries": self.generation_max_retries,
        }
