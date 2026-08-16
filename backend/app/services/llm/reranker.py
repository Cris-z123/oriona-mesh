"""可选 Reranker 用例适配器（T070 / model-egress.md Reranker 评分响应）。

- 只依赖内部 :class:`ModelGatewayService`，声明 ``rerank`` 调用类型；
- 10 秒超时与 1 次重试只由网关执行，业务适配器只消费最终成功或失败；
- ``MODEL_GATEWAY_RERANK_MODEL`` 为空时禁用重排并直接使用 RRF，且不调用网关；
- 网关最终失败（含脱敏失败零外发）返回 None，由检索服务整体回退原 RRF 顺序；
- 评分完整性校验（缺项/重复/越界/非有限）在网关适配层已严格拒绝；此处防御性
  复查，不完整时同样整体回退，不应用部分评分。
"""

import math
import uuid

from app.core.settings import Settings, get_settings
from app.infrastructure.model_gateway.config import ModelGatewaySettings
from app.infrastructure.model_gateway.service import ModelGatewayService
from app.infrastructure.model_gateway.types import (
    GatewayError,
    RerankResult,
    RerankScore,
)
from app.repositories.chunks import RetrievalChunk
from app.services.llm.base import build_model_call


def validate_rerank_scores(scores: list[RerankScore], count: int) -> bool:
    """评分完整性：每项恰好出现一次、零基序号合法、分数有限。"""
    if len(scores) != count:
        return False
    seen: set[int] = set()
    for score in scores:
        if not isinstance(score.candidate_index, int):
            return False
        if score.candidate_index < 0 or score.candidate_index >= count:
            return False
        if score.candidate_index in seen:
            return False
        if not math.isfinite(score.score):
            return False
        seen.add(score.candidate_index)
    return True


class RerankerService:
    """重排用例适配器；返回可应用评分或 None（禁用/失败回退 RRF）。"""

    def __init__(
        self,
        gateway: ModelGatewayService | None = None,
        model_gateway: ModelGatewaySettings | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.model_gateway = model_gateway or self.settings.model_gateway
        self.gateway = gateway or ModelGatewayService(self.model_gateway)

    def rerank_scores(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        candidates: list[RetrievalChunk],
    ) -> list[RerankScore] | None:
        """返回可应用的评分列表；未配置、网关失败或评分不完整时返回 None。"""
        if not self.model_gateway.reranker_enabled:
            return None
        if not candidates:
            return None
        content = f"用户问题：{query}\n候选：\n" + "\n".join(
            f"[{i}] {c.content}" for i, c in enumerate(candidates)
        )
        call = build_model_call("rerank", content, user_id=user_id, settings=self.settings)
        try:
            result = self.gateway.call(call)
        except GatewayError:
            return None
        if not isinstance(result, RerankResult):
            return None
        scores = list(result.scores)
        if not validate_rerank_scores(scores, len(candidates)):
            return None
        return scores
