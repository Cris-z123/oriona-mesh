"""嵌入用例适配器（T055 / FR-027、FR-028）。

- 只依赖内部 :class:`ModelGatewayService`，声明 embedding 调用类型；
- 每段文本恰好一次网关调用：超时与最多 2 次重试仅由网关执行，业务适配器
  不得再次重试（网关只返回最终成功或失败）；
- 默认 ``text-embedding-3-small``（1536 维）并校验最终向量维度；维度不符
  收敛为嵌入失败；
- 网关任何最终失败（含脱敏失败 fail-closed 零外发）由业务适配器收敛为
  :class:`EmbeddingFailure`（领域降级：资料与任务失败）。
"""

import uuid

from app.api.middleware.trace import current_trace_id
from app.core.settings import Settings, get_settings
from app.infrastructure.model_gateway.service import ModelGatewayService
from app.infrastructure.model_gateway.types import EmbeddingResult, GatewayError, ModelCall
from app.infrastructure.rate_limit.keys import user_fingerprint

# text-embedding-3-small 默认维度；变更维度必须迁移并重建向量（quickstart）。
EMBEDDING_DIMENSION = 1536

EMBEDDING_FAILED_MSG = "资料向量化失败，请删除后重新上传"


class EmbeddingFailure(Exception):
    """嵌入失败（业务适配器领域降级；对应 ``20012``）。"""

    def __init__(self, message: str = EMBEDDING_FAILED_MSG) -> None:
        self.message = message
        super().__init__(message)


class EmbeddingService:
    """嵌入用例适配器。"""

    def __init__(
        self,
        gateway: ModelGatewayService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.gateway = gateway or ModelGatewayService(self.settings.model_gateway)

    def embed_texts(
        self,
        texts: list[str],
        *,
        user_id,
        trace_id: str | None = None,
    ) -> list[list[float]]:
        """为每段文本生成 1536 维向量；任何失败收敛为 :class:`EmbeddingFailure`。"""
        secret = self.settings.rate_limit.subject_hmac_key.get_secret_value()
        digest = user_fingerprint(str(user_id), secret)
        tid = trace_id or current_trace_id() or str(uuid.uuid4())
        vectors: list[list[float]] = []
        for text in texts:
            call = ModelCall(
                call_id=str(uuid.uuid4()),
                trace_id=tid,
                subject_digest=digest,
                call_type="embedding",
                content=text,
            )
            try:
                result = self.gateway.call(call)
            except GatewayError as exc:
                raise EmbeddingFailure() from exc
            if not isinstance(result, EmbeddingResult) or not result.vectors:
                raise EmbeddingFailure()
            for raw in result.vectors:
                vector = list(raw)
                if len(vector) != EMBEDDING_DIMENSION:
                    raise EmbeddingFailure()
                vectors.append(vector)
        return vectors


def default_embedding_service() -> EmbeddingService:
    return EmbeddingService()
