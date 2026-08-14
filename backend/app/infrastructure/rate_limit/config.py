"""分级限流配置（quickstart 限流配置契约）。

字段名对应 ``RATE_LIMIT_*`` 环境变量；默认值仅用于本地开发，部署必须显式覆盖。
本模块只定义配置模型，不执行限流；统一必填校验在 ``app.core.readiness`` 完成。
"""

import ipaddress

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RateLimitSettings(BaseSettings):
    """限流阈值/窗口、主体 HMAC 密钥、只读 fail-open 与可信代理配置。"""

    model_config = SettingsConfigDict(env_prefix="RATE_LIMIT_", extra="ignore")

    # 主体摘要密钥：对账号、用户和租户标识生成不可逆限流键；必填且不得与 JWT/供应商凭证复用。
    subject_hmac_key: SecretStr = SecretStr("")
    # 可信反向代理 CIDR 逗号列表；为空时忽略全部转发头并使用直连对端 IP。
    trusted_proxy_cidrs: str = ""
    # 认证来源 IP 限制（20 次/5 分钟）
    auth_ip_limit: int = Field(default=20, ge=1)
    auth_ip_window_seconds: int = Field(default=300, ge=1)
    # 注册/登录按规范化邮箱 HMAC 摘要；刷新按 refresh token HMAC 指纹（5 次/5 分钟）
    auth_account_limit: int = Field(default=5, ge=1)
    auth_account_window_seconds: int = Field(default=300, ge=1)
    # 上传每用户 10 次/10 分钟
    upload_limit: int = Field(default=10, ge=1)
    upload_window_seconds: int = Field(default=600, ge=1)
    # 问答每用户 20 次/分钟
    question_limit: int = Field(default=20, ge=1)
    question_window_seconds: int = Field(default=60, ge=1)
    # 其他已认证接口每用户 120 次/分钟
    default_limit: int = Field(default=120, ge=1)
    default_window_seconds: int = Field(default=60, ge=1)
    # Redis 不可用时只读 GET 是否降级放行；状态变更始终 fail-closed。
    read_fail_open: bool = True

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _validate_cidrs(cls, value: str) -> str:
        for raw in (part.strip() for part in value.split(",") if part.strip()):
            try:
                ipaddress.ip_network(raw, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy CIDR: {raw!r}") from exc
        return value

    @property
    def trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """解析后的可信代理 CIDR 网络列表（供 T027 来源 IP 解析使用）。"""
        return [
            ipaddress.ip_network(raw.strip(), strict=False)
            for raw in (
                part.strip() for part in self.trusted_proxy_cidrs.split(",") if part.strip()
            )
        ]
