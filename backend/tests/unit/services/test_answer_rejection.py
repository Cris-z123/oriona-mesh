"""可信拒答失败测试（T062 / FR-015、FR-017）。

覆盖：无完成资料时 ``20005/409`` 且零消息副作用；两路门槛过滤与融合后为空时直接
可信拒答，不调用生成模型、不创建 Citation、assistant 收敛为 ``completed/stop``；
有证据路径仍调用生成并保存完整引用字段；生成失败收敛 ``failed/error``；
客户端断开收敛 ``cancelled/cancelled``。
"""

import uuid

import pytest

from app.api.middleware.errors import ApiError
from app.infrastructure.model_gateway.types import GatewayError
from app.models.enums import MessageFinishReason, MessageStatus
from app.repositories.chunks import RetrievalChunk
from app.services.answer_service import (
    NO_EVIDENCE_CONTENT,
    AnswerService,
    EvidenceBundle,
    NoEvidenceAnswer,
)
from app.services.llm.chat import GenerationFailure
from app.services.retrieval_service import RetrievalResult

pytestmark = pytest.mark.unit

_KB_NOT_READY_MSG = "当前知识库没有已完成资料，请上传或等待资料处理完成"


class FakeConversations:
    """进程内消息仓库替身：记录创建与终态收敛。"""

    def __init__(self, history: list[tuple[str, str]] | None = None) -> None:
        self.history = history or []
        self.user_messages: list[dict] = []
        self.assistant_messages: list[dict] = []

    def create_user_message(
        self, *, user_id, conversation_id, content, rewritten_query: str | None = None
    ) -> None:
        self.user_messages.append(
            {"id": uuid.uuid4(), "content": content, "rewritten_query": rewritten_query}
        )

    def create_streaming_assistant_message(self, *, user_id, conversation_id) -> uuid.UUID:
        message_id = uuid.uuid4()
        self.assistant_messages.append(
            {
                "id": message_id,
                "status": MessageStatus.STREAMING,
                "finish_reason": None,
                "content": "",
            }
        )
        return message_id

    def set_terminal(
        self,
        *,
        message_id,
        user_id,
        status: MessageStatus,
        finish_reason: MessageFinishReason | None,
        content: str | None = None,
    ) -> None:
        for message in self.assistant_messages:
            if message["id"] == message_id:
                if message["status"] != MessageStatus.STREAMING:
                    return  # 已收敛终态不可覆盖（与 MessageTerminalState 一致）
                message["status"] = status
                message["finish_reason"] = finish_reason
                if content is not None:
                    message["content"] = content
                return
        raise AssertionError("unknown message id")

    def recent_history(self, *, user_id, conversation_id, turns: int) -> list[tuple[str, str]]:
        return self.history[-turns:]

    def update_last_message_at(self, *, user_id, conversation_id) -> None:
        pass


class FakeRetrieval:
    def __init__(self, *, retrievable: int = 1, result: RetrievalResult | None = None) -> None:
        self.retrievable = retrievable
        self.result = result or RetrievalResult(query="q", candidates=(), context_pack="")
        self.calls: list[tuple[uuid.UUID, uuid.UUID, str]] = []

    def count_retrievable(self, user_id, knowledge_base_id) -> int:
        return self.retrievable

    def retrieve(self, user_id, knowledge_base_id, query, *, trace_id=None) -> RetrievalResult:
        self.calls.append((user_id, knowledge_base_id, query))
        return self.result


class FakeRewrite:
    def __init__(self, result: str | None = None, error: bool = False) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, list]] = []

    def rewrite(self, *, user_id, query, history) -> str:
        self.calls.append((query, history))
        if self.error:
            return query  # 改写失败回退原问题
        return self.result or query


class FakeGeneration:
    def __init__(self, deltas: list[str] | None = None, error: GatewayError | None = None) -> None:
        self.deltas = deltas if deltas is not None else ["根据", "资料"]
        self.error = error
        self.calls = 0

    def stream(self, *, user_id, query, context_pack, history):
        self.calls += 1
        if self.error is not None:
            raise GenerationFailure() from self.error
        yield from self.deltas


class FakeCitations:
    def __init__(self) -> None:
        self.saved: list[list] = []

    def save(self, *, message_id, user_id, knowledge_base_id, drafts) -> None:
        self.saved.append(list(drafts))


def _service(
    retrieval: FakeRetrieval | None = None,
    generation=None,
    rewrite=None,
    citations=None,
    conversations=None,
) -> AnswerService:
    return AnswerService(
        conversations=conversations or FakeConversations(),
        retrieval=retrieval or FakeRetrieval(),
        rewrite=rewrite or FakeRewrite(),
        generation=generation,
        citations=citations or FakeCitations(),
    )


def _chunk(seq: int = 0) -> RetrievalChunk:
    return RetrievalChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_version=1,
        seq=seq,
        content="来源内容",
        page=3,
        section="章节",
        filename="source.txt",
        file_type="txt",
        vector_similarity=0.9,
        fused_score=0.9,
    )


class TestKnowledgeBaseNotReady:
    def test_no_completed_documents_raises_20005_without_side_effects(self) -> None:
        conversations = FakeConversations()
        generation = FakeGeneration()
        service = _service(
            conversations=conversations,
            retrieval=FakeRetrieval(retrievable=0),
            generation=generation,
        )
        with pytest.raises(ApiError) as excinfo:
            service.prepare(
                user_id=uuid.uuid4(),
                knowledge_base_id=uuid.uuid4(),
                conversation_id=uuid.uuid4(),
                content="问题",
            )
        assert excinfo.value.code == 20005
        assert excinfo.value.http_status == 409
        assert excinfo.value.message == _KB_NOT_READY_MSG
        # 零业务副作用：不创建消息、不调用生成模型。
        assert conversations.user_messages == []
        assert conversations.assistant_messages == []
        assert generation.calls == 0


class TestNoEvidenceRejection:
    def _empty_result(self) -> RetrievalResult:
        return RetrievalResult(query="q", candidates=(), context_pack="")

    def test_no_evidence_does_not_call_generation_and_no_citations(self) -> None:
        conversations = FakeConversations()
        generation = FakeGeneration()
        citations = FakeCitations()
        rewrite = FakeRewrite(result="改写")
        service = _service(
            conversations=conversations,
            retrieval=FakeRetrieval(result=self._empty_result()),
            rewrite=rewrite,
            generation=generation,
            citations=citations,
        )
        outcome = service.prepare(
            user_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            content="不相关问题",
        )
        assert isinstance(outcome, NoEvidenceAnswer)
        assert outcome.no_evidence is True
        assert outcome.content == NO_EVIDENCE_CONTENT
        # 不调用生成模型、不创建引用。
        assert generation.calls == 0
        assert citations.saved == []
        # 用户消息保留改写结果；assistant 收敛 completed/stop。
        assert conversations.user_messages[0]["rewritten_query"] == "改写"
        terminal = conversations.assistant_messages[0]
        assert terminal["status"] == MessageStatus.COMPLETED
        assert terminal["finish_reason"] == MessageFinishReason.STOP
        assert terminal["content"] == NO_EVIDENCE_CONTENT

    def test_rewrite_failure_falls_back_to_original_query_and_still_rejects(self) -> None:
        conversations = FakeConversations()
        generation = FakeGeneration()
        service = _service(
            conversations=conversations,
            retrieval=FakeRetrieval(result=self._empty_result()),
            rewrite=FakeRewrite(error=True),
            generation=generation,
        )
        service.prepare(
            user_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            content="原问题",
        )
        assert conversations.user_messages[0]["rewritten_query"] == "原问题"
        assert generation.calls == 0
        assert conversations.assistant_messages[0]["status"] == MessageStatus.COMPLETED


class TestPrepareFailureConvergence:
    def test_retrieval_failure_converges_streaming_to_failed_error(self) -> None:
        """prepare 在消息创建后失败（如检索异常）不得遗留 streaming（T074）。"""
        conversations = FakeConversations()

        class RaisingRetrieval(FakeRetrieval):
            def retrieve(self, user_id, knowledge_base_id, query, *, trace_id=None):
                raise RuntimeError("retrieval exploded")

        service = _service(
            conversations=conversations,
            retrieval=RaisingRetrieval(),
            rewrite=FakeRewrite(),
        )
        with pytest.raises(RuntimeError):
            service.prepare(
                user_id=uuid.uuid4(),
                knowledge_base_id=uuid.uuid4(),
                conversation_id=uuid.uuid4(),
                content="问题",
            )
        # 用户消息保留，assistant 立即收敛 failed/error（不依赖维护扫描器）。
        assert len(conversations.user_messages) == 1
        terminal = conversations.assistant_messages[0]
        assert terminal["status"] == MessageStatus.FAILED
        assert terminal["finish_reason"] == MessageFinishReason.ERROR


class TestEvidencePath:
    def test_evidence_calls_generation_and_completes_with_citations(self) -> None:
        conversations = FakeConversations()
        generation = FakeGeneration(deltas=["根据", "资料"])
        citations = FakeCitations()
        result = RetrievalResult(query="q", candidates=(_chunk(),), context_pack="来源内容")
        service = _service(
            conversations=conversations,
            retrieval=FakeRetrieval(result=result),
            generation=generation,
            citations=citations,
        )
        bundle = service.prepare(
            user_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            content="问题",
        )
        assert isinstance(bundle, EvidenceBundle)
        assert bundle.no_evidence is False
        assert bundle.citations[0].rank == 1
        assert bundle.citations[0].score == 0.9
        assert bundle.citations[0].snapshot["filename"] == "source.txt"
        assert bundle.citations[0].snapshot["page"] == 3

        deltas = list(service.stream_generation(bundle))
        assert deltas == ["根据", "资料"]
        assert generation.calls == 1
        service.complete(bundle, content="根据资料", finish_reason="stop")
        assert citations.saved == [list(bundle.citations)]
        terminal = conversations.assistant_messages[0]
        assert terminal["status"] == MessageStatus.COMPLETED
        assert terminal["finish_reason"] == MessageFinishReason.STOP
        assert terminal["content"] == "根据资料"

    def test_generation_failure_converges_failed_error(self) -> None:
        conversations = FakeConversations()
        citations = FakeCitations()
        result = RetrievalResult(query="q", candidates=(_chunk(),), context_pack="来源内容")
        service = _service(
            conversations=conversations,
            retrieval=FakeRetrieval(result=result),
            generation=FakeGeneration(error=GatewayError("provider_error", "provider failed")),
            citations=citations,
        )
        bundle = service.prepare(
            user_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            content="问题",
        )
        assert isinstance(bundle, EvidenceBundle)
        with pytest.raises(GenerationFailure):
            list(service.stream_generation(bundle))
        service.fail(bundle.message_id, user_id=bundle.user_id)
        terminal = conversations.assistant_messages[0]
        assert terminal["status"] == MessageStatus.FAILED
        assert terminal["finish_reason"] == MessageFinishReason.ERROR
        # 失败路径不保存引用。
        assert citations.saved == []

    def test_client_disconnect_converges_cancelled(self) -> None:
        conversations = FakeConversations()
        service = _service(
            conversations=conversations,
            retrieval=FakeRetrieval(
                result=RetrievalResult(query="q", candidates=(_chunk(),), context_pack="c")
            ),
            generation=FakeGeneration(deltas=["x"]),
        )
        bundle = service.prepare(
            user_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            content="问题",
        )
        assert isinstance(bundle, EvidenceBundle)
        service.cancel(bundle.message_id, user_id=bundle.user_id)
        terminal = conversations.assistant_messages[0]
        assert terminal["status"] == MessageStatus.CANCELLED
        assert terminal["finish_reason"] == MessageFinishReason.CANCELLED

    def test_late_terminal_write_does_not_overwrite_cancelled(self) -> None:
        """断开后继续驱动流不得覆盖已收敛终态（幂等终态收敛）。"""
        conversations = FakeConversations()
        service = _service(
            conversations=conversations,
            retrieval=FakeRetrieval(
                result=RetrievalResult(query="q", candidates=(_chunk(),), context_pack="c")
            ),
            generation=FakeGeneration(deltas=["x"]),
        )
        bundle = service.prepare(
            user_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            content="问题",
        )
        assert isinstance(bundle, EvidenceBundle)
        service.cancel(bundle.message_id, user_id=bundle.user_id)
        service.complete(bundle, content="late", finish_reason="stop")
        terminal = conversations.assistant_messages[0]
        # 已收敛 cancelled 不得被迟到完成覆盖。
        assert terminal["status"] == MessageStatus.CANCELLED
        assert terminal["finish_reason"] == MessageFinishReason.CANCELLED
