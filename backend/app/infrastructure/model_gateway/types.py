"""模型出口调用类型与结果（model-egress.md 调用信封）。

- 业务调用方只能提交 :class:`ModelCall`（最小必要内容），不得指定供应商凭证；
- 供应商适配层只接受 :class:`SanitizedModelCall`（sanitization_status=passed）；
- 网关只返回最终成功或失败；领域降级由业务适配器执行。
"""

from dataclasses import dataclass, field
from typing import Any, Literal

CallType = Literal["embedding", "query_rewrite", "rerank", "generation"]

# 稳定失败分类（T035）：业务适配器据此执行领域降级或终态收敛。
GatewayErrorClass = Literal[
    "configuration",
    "sanitization_failed",
    "network",
    "provider_error",
    "timeout",
    "rate_limited",
    "invalid_response",
]


class GatewayError(Exception):
    """网关最终失败；携带稳定失败分类，不携带供应商原始响应或正文。"""

    def __init__(self, error_class: GatewayErrorClass, message: str) -> None:
        self.error_class: GatewayErrorClass = error_class
        super().__init__(message)


@dataclass(frozen=True)
class ModelCall:
    """业务调用方提交的最小调用上下文。

    content 为当前调用类型所需的最小内容；options 为与调用类型相关的非敏感参数，
    不得包含供应商凭证或请求头。frozen 语义下 options 在构造时复制，调用方后续
    修改自己的字典不影响本对象。
    """

    call_id: str
    trace_id: str
    subject_digest: str  # 不可逆用户/租户摘要
    call_type: CallType
    content: str
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 防御性浅拷贝：frozen 只阻止属性重赋值，不阻止外部可变字典内容被修改；
        # 网关与适配器读取的 options 必须与调用方字典隔离（model-egress.md）。
        object.__setattr__(self, "options", dict(self.options))


@dataclass(frozen=True)
class SanitizedModelCall:
    """脱敏后进入供应商适配层的调用对象（前置条件全部满足才可发送）。"""

    call_id: str
    trace_id: str
    subject_digest: str
    call_type: CallType
    sanitization_status: Literal["passed"]
    policy_version: str
    provider: str
    model: str
    sanitized_content: str
    options: dict[str, Any]
    timeout_seconds: float
    max_retries: int
    # generation 专用：首 token 与总时长超时（网关执行超时与重试的唯一场所）。
    first_token_timeout_seconds: float | None = None
    total_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        # 与 ModelCall 相同的引用隔离：供应商适配层读取的 options 不得与任何
        # 外部字典共享（frozen 语义的完整承诺）。
        object.__setattr__(self, "options", dict(self.options))


@dataclass(frozen=True)
class RerankScore:
    """重排评分项：零基候选临时序号与有限数值分数。"""

    candidate_index: int
    score: float


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]


@dataclass(frozen=True)
class QueryRewriteResult:
    rewritten_query: str


@dataclass(frozen=True)
class RerankResult:
    scores: list[RerankScore]


@dataclass(frozen=True)
class GenerationResult:
    content: str
    finish_reason: Literal["stop", "length"]


@dataclass(frozen=True)
class GenerationDelta:
    """流式生成增量（SSE delta 使用）；流结束时由 finish_reason 表达。"""

    text: str
    finish_reason: Literal["stop", "length", None] = None


ModelCallResult = EmbeddingResult | QueryRewriteResult | RerankResult | GenerationResult
