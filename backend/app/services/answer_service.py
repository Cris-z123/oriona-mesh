"""可信问答编排服务（T071 / FR-014、FR-015、FR-017、FR-018）。

- 知识库没有已完成资料时同步拒绝 ``20005/409``，且不创建任何消息副作用；
- 提问消息（completed）与 assistant 流式消息（streaming）在同一提交前创建；
- 查询改写只使用最近三轮上下文，最终失败回退原问题；检索只消费通过证据门槛的
  候选；两路过滤与融合后为空时直接可信拒答（``completed/stop`` + 固定提示），
  不调用生成模型、不创建引用；
- 有证据时流式生成由 :class:`GenerationPort` 提供；生成最终失败由
  :class:`GenerationFailure` 表达，问答层收敛 ``failed/error``；客户端断开收敛
  ``cancelled/cancelled``；终态收敛经 :class:`MessageTerminalState` 单向保证。
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from app.api.middleware.errors import ApiError
from app.api.middleware.trace import current_trace_id
from app.infrastructure.model_gateway.types import GenerationDelta
from app.models.enums import MessageFinishReason, MessageStatus
from app.repositories.conversations import CitationDraft
from app.services.citation_service import build_citation_drafts
from app.services.llm.chat import GenerationFailure
from app.services.retrieval_service import RetrievalResult

# 可信无证据答复（FR-017：明确告知未找到证据，不编造知识库结论）。
NO_EVIDENCE_CONTENT = "当前知识库中没有找到相关证据，请尝试更换问法或检查资料处理状态。"
KNOWLEDGE_BASE_NOT_READY_MSG = "当前知识库没有已完成资料，请上传或等待资料处理完成"

# 改写与生成共用的最近上下文轮数（plan.md 决策 4）。
HISTORY_TURNS = 3


class KnowledgeBaseNotReady(ApiError):
    """知识库没有已完成资料（20005/409），消息发送前同步拒绝。"""

    def __init__(self) -> None:
        super().__init__(20005, KNOWLEDGE_BASE_NOT_READY_MSG, 409)


class ConversationPort(Protocol):
    """会话/消息持久化端口（由 ConversationService 实现）。"""

    def create_user_message(
        self, *, user_id, conversation_id, content, rewritten_query=None
    ) -> None: ...

    def create_streaming_assistant_message(self, *, user_id, conversation_id) -> uuid.UUID: ...

    def set_terminal(self, *, message_id, user_id, status, finish_reason, content=None) -> None: ...

    def recent_history(self, *, user_id, conversation_id, turns) -> list[tuple[str, str]]: ...

    def update_last_message_at(self, *, user_id, conversation_id) -> None: ...


class RetrievalPort(Protocol):
    """检索端口（由 RetrievalService 实现）。"""

    def count_retrievable(self, user_id, knowledge_base_id) -> int: ...

    def retrieve(self, user_id, knowledge_base_id, query, *, trace_id=None) -> RetrievalResult: ...


class RewritePort(Protocol):
    """查询改写端口；最终失败回退原问题（由 QueryRewriteService 实现）。"""

    def rewrite(self, *, user_id, query, history) -> str: ...


class GenerationPort(Protocol):
    """生成端口；最终失败抛 GenerationFailure（由 GenerationService 实现）。"""

    def stream(self, *, user_id, query, context_pack, history) -> Iterator[GenerationDelta]: ...


class CitationPort(Protocol):
    """引用保存端口（由 CitationService 实现）。"""

    def save(self, *, message_id, user_id, knowledge_base_id, drafts) -> None: ...


@dataclass(frozen=True)
class NoEvidenceAnswer:
    """可信无证据答复（completed/stop，不调用生成模型）。"""

    message_id: uuid.UUID
    user_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    no_evidence: bool = True
    content: str = NO_EVIDENCE_CONTENT
    citations: tuple[CitationDraft, ...] = ()  # 无证据路径零引用


@dataclass(frozen=True)
class EvidenceBundle:
    """有证据回答上下文：消息、改写、上下文包与待保存引用。"""

    message_id: uuid.UUID
    user_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    no_evidence: bool = False
    rewritten_query: str = ""
    context_pack: str = ""
    history: tuple[tuple[str, str], ...] = ()
    citations: tuple[CitationDraft, ...] = ()

    def citation_previews(self) -> list[dict]:
        from app.services.citation_service import citation_preview_dtos

        return citation_preview_dtos(list(self.citations))


class AnswerService:
    """问答编排：准备（20005/改写/检索/消息创建）与终态收敛。"""

    def __init__(
        self,
        *,
        conversations: ConversationPort,
        retrieval: RetrievalPort,
        rewrite: RewritePort,
        generation: GenerationPort | None = None,
        citations: CitationPort | None = None,
    ) -> None:
        self.conversations = conversations
        self.retrieval = retrieval
        self.rewrite = rewrite
        self.generation = generation
        self.citations = citations

    # ------------------------------------------------------------------
    # 准备
    # ------------------------------------------------------------------
    def prepare(
        self,
        *,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        conversation_id: uuid.UUID,
        content: str,
    ) -> NoEvidenceAnswer | EvidenceBundle:
        """校验知识库就绪 → 创建消息 → 改写 → 检索 → 返回无证据或证据包。"""
        if self.retrieval.count_retrievable(user_id, knowledge_base_id) == 0:
            raise KnowledgeBaseNotReady()
        history = self.conversations.recent_history(
            user_id=user_id, conversation_id=conversation_id, turns=HISTORY_TURNS
        )
        rewritten = self.rewrite.rewrite(user_id=user_id, query=content, history=history)
        self.conversations.create_user_message(
            user_id=user_id,
            conversation_id=conversation_id,
            content=content,
            rewritten_query=rewritten,
        )
        message_id = self.conversations.create_streaming_assistant_message(
            user_id=user_id, conversation_id=conversation_id
        )
        try:
            result = self.retrieval.retrieve(
                user_id,
                knowledge_base_id,
                rewritten,
                trace_id=current_trace_id() or None,
            )
        except Exception:
            # 检索异常不得遗留 streaming：立即收敛 failed/error 后重抛（T074）。
            try:
                self.conversations.set_terminal(
                    message_id=message_id,
                    user_id=user_id,
                    status=MessageStatus.FAILED,
                    finish_reason=MessageFinishReason.ERROR,
                )
            except Exception:
                pass  # 终态收敛失败（如数据库不可用）时以原检索异常为准
            raise
        if not result.candidates:
            self.conversations.set_terminal(
                message_id=message_id,
                user_id=user_id,
                status=MessageStatus.COMPLETED,
                finish_reason=MessageFinishReason.STOP,
                content=NO_EVIDENCE_CONTENT,
            )
            return NoEvidenceAnswer(
                message_id=message_id,
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
            )
        drafts = tuple(build_citation_drafts(list(result.candidates)))
        return EvidenceBundle(
            message_id=message_id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            rewritten_query=rewritten,
            context_pack=result.context_pack,
            history=tuple(history),
            citations=drafts,
        )

    # ------------------------------------------------------------------
    # 生成与终态收敛
    # ------------------------------------------------------------------
    def stream_generation(self, bundle: EvidenceBundle) -> Iterator[GenerationDelta]:
        """流式生成（仅网关执行超时与重试；最终失败抛 GenerationFailure）。"""
        if self.generation is None:
            raise GenerationFailure()
        yield from self.generation.stream(
            user_id=bundle.user_id,
            query=bundle.rewritten_query,
            context_pack=bundle.context_pack,
            history=list(bundle.history),
        )

    def complete(self, bundle, *, content: str, finish_reason: str) -> None:
        """正常完成（含可信无证据）：assistant completed/stop|length + 保存引用。"""
        self.conversations.set_terminal(
            message_id=bundle.message_id,
            user_id=bundle.user_id,
            status=MessageStatus.COMPLETED,
            finish_reason=MessageFinishReason(finish_reason),
            content=content,
        )
        if self.citations is not None and bundle.citations:
            self.citations.save(
                message_id=bundle.message_id,
                user_id=bundle.user_id,
                knowledge_base_id=bundle.knowledge_base_id,
                drafts=list(bundle.citations),
            )

    def fail(self, message_id: uuid.UUID, *, user_id: uuid.UUID) -> None:
        """服务/供应商错误重试耗尽：assistant failed/error。"""
        self.conversations.set_terminal(
            message_id=message_id,
            user_id=user_id,
            status=MessageStatus.FAILED,
            finish_reason=MessageFinishReason.ERROR,
        )

    def cancel(self, message_id: uuid.UUID, *, user_id: uuid.UUID) -> None:
        """客户端连接断开：assistant cancelled/cancelled。"""
        self.conversations.set_terminal(
            message_id=message_id,
            user_id=user_id,
            status=MessageStatus.CANCELLED,
            finish_reason=MessageFinishReason.CANCELLED,
        )
