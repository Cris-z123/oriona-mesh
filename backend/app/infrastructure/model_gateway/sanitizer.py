"""模型出口脱敏器（FR-028 / model-egress.md）。

:func:`sanitize_call` 在外部请求发送前对 :class:`ModelCall` 的最小内容与选项执行
脱敏；任何脱敏异常都必须 fail-closed（不生成外部请求），且不得在日志或审计中记录
触发失败的原始内容。脱敏后的内容只用于本次网络调用，不写入调用日志。
"""

from dataclasses import dataclass
from typing import Any

from app.infrastructure.model_gateway.policies.v1 import SanitizationError, build_policy
from app.infrastructure.model_gateway.types import ModelCall

__all__ = ["SanitizationError", "SanitizedContent", "sanitize_call"]


@dataclass(frozen=True)
class SanitizedContent:
    """脱敏后的外发内容与非敏感选项。"""

    content: str
    options: dict[str, Any]


def sanitize_call(call: ModelCall, policy_version: str) -> SanitizedContent:
    """对调用内容执行最小化与脱敏。

    :raises SanitizationError: 脱敏失败（fail-closed），调用方不得发起外部请求。
    """
    try:
        policy = build_policy(policy_version)
        sanitized_content = policy.sanitize_text(call.content)
        sanitized_options = policy.sanitize_options(call.options)
    except SanitizationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何脱敏异常都必须 fail-closed
        raise SanitizationError(f"sanitizer failed: {type(exc).__name__}") from exc

    return SanitizedContent(content=sanitized_content, options=sanitized_options)
