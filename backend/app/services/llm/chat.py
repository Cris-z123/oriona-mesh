"""查询改写与回答生成用例适配器（T071 / model-egress.md 各调用类型允许内容）。

- 只依赖内部 :class:`ModelGatewayService`；改写 10 秒/1 次、生成首 token 15 秒/
  总时长 120 秒/1 次的超时重试只由网关执行，业务适配器不得包装第二层尝试；
- 内容按调用类型最小化：改写只发送当前问题与最近三轮必要对话文本；生成发送
  问题、脱敏 Context Pack 与最近三轮对话文本；内部 UUID、文件名与路径不进入
  content（由网关脱敏器兜底拒绝）；
- 领域降级：改写最终失败回退原问题；生成最终失败抛 :class:`GenerationFailure`，
  由问答服务收敛 assistant 为 ``failed/error``（不得误记为取消）。
"""

import uuid
from collections.abc import Generator

from app.core.settings import Settings, get_settings
from app.infrastructure.model_gateway.service import ModelGatewayService
from app.infrastructure.model_gateway.types import (
    GatewayError,
    GenerationDelta,
    QueryRewriteResult,
)
from app.services.llm.base import build_model_call


class GenerationFailure(Exception):
    """生成最终失败（领域降级：assistant 收敛 failed/error）。"""


def _history_text(history: list[tuple[str, str]]) -> str:
    return "\n".join(f"{role}：{text}" for role, text in history)


class QueryRewriteService:
    """查询改写用例适配器；最终失败回退原问题。"""

    def __init__(
        self,
        gateway: ModelGatewayService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.gateway = gateway or ModelGatewayService(self.settings.model_gateway)

    def rewrite(self, *, user_id: uuid.UUID, query: str, history: list[tuple[str, str]]) -> str:
        content = f"用户问题：{query}\n最近对话：\n{_history_text(history)}"
        call = build_model_call("query_rewrite", content, user_id=user_id, settings=self.settings)
        try:
            result = self.gateway.call(call)
        except GatewayError:
            return query
        if not isinstance(result, QueryRewriteResult) or not result.rewritten_query:
            return query
        return result.rewritten_query


class GenerationService:
    """回答生成用例适配器；最终失败抛 :class:`GenerationFailure`。"""

    def __init__(
        self,
        gateway: ModelGatewayService | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.gateway = gateway or ModelGatewayService(self.settings.model_gateway)

    def stream(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        context_pack: str,
        history: list[tuple[str, str]],
    ) -> Generator[GenerationDelta, None, None]:
        content = (
            f"用户问题：{query}\n\n知识库上下文：\n{context_pack}\n\n最近对话：\n"
            f"{_history_text(history)}"
        )
        call = build_model_call("generation", content, user_id=user_id, settings=self.settings)
        try:
            yield from self.gateway.call_stream(call)
        except GatewayError as exc:
            raise GenerationFailure() from exc
