"""端点限流策略注册表（FR-026 / openapi.yaml x-rate-limit-policy）。

策略名与 OpenAPI 的 ``x-rate-limit-policy`` 一一对应：
- ``auth-ip-and-account``：注册/登录，同时应用来源 IP 与规范化邮箱 HMAC 摘要；
- ``refresh-ip-and-token``：刷新会话，同时应用来源 IP 与 refresh token HMAC 指纹；
- ``upload-user``：批量上传，按当前用户不可逆摘要；
- ``question-user``：发送问答，按当前用户不可逆摘要；
- ``authenticated-default``：其他已认证接口，按当前用户不可逆摘要。

阈值与窗口来自唯一根配置（RATE_LIMIT_*），可在部署时覆盖。
"""

from dataclasses import dataclass
from typing import Literal

from app.infrastructure.rate_limit.config import RateLimitSettings

SubjectKind = Literal["ip", "account", "refresh_token", "user"]

# 与 openapi.yaml x-rate-limit-policy 对齐的端点策略名。
POLICY_AUTH_IP_AND_ACCOUNT = "auth-ip-and-account"
POLICY_REFRESH_IP_AND_TOKEN = "refresh-ip-and-token"
POLICY_UPLOAD_USER = "upload-user"
POLICY_QUESTION_USER = "question-user"
POLICY_AUTHENTICATED_DEFAULT = "authenticated-default"

POLICY_NAMES = (
    POLICY_AUTH_IP_AND_ACCOUNT,
    POLICY_REFRESH_IP_AND_TOKEN,
    POLICY_UPLOAD_USER,
    POLICY_QUESTION_USER,
    POLICY_AUTHENTICATED_DEFAULT,
)


@dataclass(frozen=True)
class RateLimitRule:
    """单条限流规则：主体种类、阈值与窗口。"""

    subject_kind: SubjectKind
    limit: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitPolicy:
    """端点限流策略：一个端点类别可同时应用多条规则，任一超限即拒绝。"""

    name: str
    rules: tuple[RateLimitRule, ...]


def build_policies(settings: RateLimitSettings) -> dict[str, RateLimitPolicy]:
    """按配置阈值构建全部端点策略。"""
    return {
        POLICY_AUTH_IP_AND_ACCOUNT: RateLimitPolicy(
            POLICY_AUTH_IP_AND_ACCOUNT,
            (
                RateLimitRule("ip", settings.auth_ip_limit, settings.auth_ip_window_seconds),
                RateLimitRule(
                    "account", settings.auth_account_limit, settings.auth_account_window_seconds
                ),
            ),
        ),
        POLICY_REFRESH_IP_AND_TOKEN: RateLimitPolicy(
            POLICY_REFRESH_IP_AND_TOKEN,
            (
                RateLimitRule("ip", settings.auth_ip_limit, settings.auth_ip_window_seconds),
                RateLimitRule(
                    "refresh_token",
                    settings.auth_account_limit,
                    settings.auth_account_window_seconds,
                ),
            ),
        ),
        POLICY_UPLOAD_USER: RateLimitPolicy(
            POLICY_UPLOAD_USER,
            (RateLimitRule("user", settings.upload_limit, settings.upload_window_seconds),),
        ),
        POLICY_QUESTION_USER: RateLimitPolicy(
            POLICY_QUESTION_USER,
            (RateLimitRule("user", settings.question_limit, settings.question_window_seconds),),
        ),
        POLICY_AUTHENTICATED_DEFAULT: RateLimitPolicy(
            POLICY_AUTHENTICATED_DEFAULT,
            (RateLimitRule("user", settings.default_limit, settings.default_window_seconds),),
        ),
    }
