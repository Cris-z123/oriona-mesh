"""供应商适配器边界（model-egress.md 供应商适配层前置条件）。

适配器只接受 ``SanitizedModelCall``（sanitization_status=passed）；每次调用只执行
一次物理供应商请求，不实现重试（重试与超时只由网关执行）。供应商 SDK 与外部模型
HTTP 客户端只允许出现在 ``providers/`` 目录。
"""

from collections.abc import Generator
from typing import Protocol

from app.infrastructure.model_gateway.types import (
    EmbeddingResult,
    GatewayError,
    GenerationResult,
    QueryRewriteResult,
    RerankResult,
    SanitizedModelCall,
)


class ProviderAdapter(Protocol):
    """供应商适配器端口。"""

    name: str

    def embed(self, call: SanitizedModelCall) -> EmbeddingResult:
        """Embedding 调用：使用 embeddings 端点；单次物理请求。"""
        ...

    def chat(self, call: SanitizedModelCall) -> QueryRewriteResult | GenerationResult:
        """Query Rewrite / Generation 调用：使用 chat 端点；单次物理请求。"""
        ...

    def chat_stream(self, call: SanitizedModelCall) -> Generator[str, None, None]:
        """Generation 流式调用：按 token 增量产出文本片段；单次物理请求。

        返回值必须是生成器：网关在放弃流时对其调用 ``close()`` 中止物理请求。
        """
        ...

    def rerank(self, call: SanitizedModelCall) -> RerankResult:
        """Reranker 调用：chat 端点返回结构化 scores；单次物理请求。"""
        ...

    def classify_exception(self, exc: Exception) -> GatewayError:
        """把供应商/底层异常转换为稳定失败分类。

        供应商 SDK 与异常类型只允许出现在 ``providers/``（model-egress.md），
        网关通过本端口分类异常，不直接导入供应商 SDK。
        """
        ...
