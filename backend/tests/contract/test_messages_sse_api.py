"""消息 SSE 契约测试（T063 / FR-014、FR-017、FR-018、openapi.yaml messages 段）。

覆盖原始 ``event:``/``data:`` 文本帧与五类判别事件（message_start、retrieval_done、
delta、message_end、error）、``retrieval_done`` 与引用详情复用同一 Citation 字段语义、
统一信封（code=0、msg、trace_id）、正常/可信无证据收敛 ``completed/stop``、供应商/
模型/服务错误重试耗尽发送 ``error`` 且持久化 ``failed/error``、客户端连接断开收敛
``cancelled/cancelled``、``20005/409`` 与跨用户 ``20007/404`` 为普通 JSON 信封。

通过依赖覆盖注入假检索/改写/生成端口；消息与终态持久化走真实会话仓储（真实数据库）。
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.infrastructure.model_gateway.types import GatewayError, GenerationDelta
from app.models.conversation import Message
from app.models.enums import MessageFinishReason, MessageRole, MessageStatus
from app.repositories.chunks import RetrievalChunk
from app.services.answer_service import NO_EVIDENCE_CONTENT, AnswerService
from app.services.llm.chat import GenerationFailure
from app.services.retrieval_service import RetrievalResult

pytestmark = pytest.mark.contract

_KB_NOT_READY_MSG = "当前知识库没有已完成资料，请上传或等待资料处理完成"


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema(test_engine):
    """本模块部分测试不注入 db_session；依赖本夹具以触发会话级 test_engine schema 创建。"""
    yield


def _register(client: TestClient, email: str) -> dict:
    assert (
        client.post("/v1/users", json={"email": email, "password": "password123"}).status_code
        == 201
    )
    return client.post(
        "/v1/auth/sessions", json={"email": email, "password": "password123"}
    ).json()["data"]


def _headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _chunk() -> RetrievalChunk:
    return RetrievalChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_version=1,
        seq=0,
        content="来源内容预览",
        page=2,
        section="结论",
        filename="source.txt",
        file_type="txt",
        vector_similarity=0.9,
        keyword_similarity=0.8,
        fused_score=0.9,
    )


class FakeRewrite:
    def rewrite(self, *, user_id, query, history) -> str:
        return query


class FakeRetrieval:
    def __init__(self, *, retrievable: int = 1, result: RetrievalResult | None = None) -> None:
        self.retrievable = retrievable
        self.result = result

    def count_retrievable(self, user_id, knowledge_base_id) -> int:
        return self.retrievable

    def retrieve(self, user_id, knowledge_base_id, query, *, trace_id=None) -> RetrievalResult:
        return self.result or RetrievalResult(
            query=query,
            candidates=(_chunk(),),
            context_pack="来源内容预览",
        )


class FakeGeneration:
    def __init__(
        self,
        deltas: list[str] | None = None,
        error: GatewayError | None = None,
    ) -> None:
        self.deltas = deltas if deltas is not None else ["根据", "资料"]
        self.error = error
        self.calls = 0

    def stream(self, *, user_id, query, context_pack, history):
        self.calls += 1
        if self.error is not None:
            raise GenerationFailure() from self.error
        for text in self.deltas:
            yield GenerationDelta(text=text)
        yield GenerationDelta(text="", finish_reason="stop")


class FakeCitations:
    def __init__(self) -> None:
        self.saved: list[list] = []

    def save(self, *, message_id, user_id, knowledge_base_id, drafts) -> None:
        self.saved.append(list(drafts))


def _parse_sse(raw: bytes) -> list[tuple[str, dict]]:
    """把原始 ``event:``/``data:`` 文本帧解码为 (event, data) 列表。"""
    frames: list[tuple[str, dict]] = []
    for block in raw.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event, data_line = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_line = line[len("data:") :].strip()
        assert event is not None and data_line is not None, f"非法 SSE 帧: {block!r}"
        frames.append((event, json.loads(data_line)))
    return frames


def _build_answer_service(
    db: Session,
    *,
    retrieval: FakeRetrieval | None = None,
    generation: FakeGeneration | None = None,
    citations: FakeCitations | None = None,
) -> tuple[AnswerService, FakeGeneration, FakeCitations]:
    """真实会话持久化 + 假检索/改写/生成/引用端口的 AnswerService。"""
    from app.services.conversation_service import ConversationService

    citations = citations or FakeCitations()
    generation = generation or FakeGeneration()
    service = AnswerService(
        conversations=ConversationService(db),
        retrieval=retrieval or FakeRetrieval(),
        rewrite=FakeRewrite(),
        generation=generation,
        citations=citations,
    )
    return service, generation, citations


@pytest.fixture
def sse_env(client, db_session, clean_rate_limit_keys):
    """注册用户、建知识库与对话；覆盖会话与答案服务依赖，返回 (headers, conv_id, 工厂)。"""
    from app.api.v1.dependencies.auth import get_current_user
    from app.api.v1.routes.messages import get_message_answer_service
    from app.infrastructure.database.session import get_db
    from app.models.user import User

    tokens = _register(client, "sse-user@example.com")
    headers = _headers(tokens)
    resp = client.post("/v1/knowledge-bases", json={"name": "kb"}, headers=headers)
    assert resp.status_code == 201
    kb_id = uuid.UUID(resp.json()["data"]["id"])
    # 让 count_retrievable 真实生效：直接造一条 completed 资料。
    from app.models.document import Document
    from app.models.enums import DocumentStatus, FileType

    user = db_session.query(User).filter_by(email="sse-user@example.com").one()
    db_session.add(
        Document(
            user_id=user.id,
            knowledge_base_id=kb_id,
            filename="doc.txt",
            file_type=FileType.TXT,
            file_size=10,
            status=DocumentStatus.COMPLETED,
            version=1,
            storage_path="tmp/doc.txt",
            upload_batch_id=uuid.uuid4(),
            content_hash="x" * 64,
        )
    )
    db_session.commit()
    resp = client.post("/v1/conversations", json={"knowledge_base_id": str(kb_id)}, headers=headers)
    assert resp.status_code == 201
    conv_id = uuid.UUID(resp.json()["data"]["id"])

    overrides = {}
    overrides[get_db] = lambda: db_session
    overrides[get_current_user] = lambda: (
        db_session.query(User).filter_by(email="sse-user@example.com").one()
    )

    def _install(retrieval=None, generation=None, citations=None):
        service, gen, cite = _build_answer_service(
            db_session, retrieval=retrieval, generation=generation, citations=citations
        )
        overrides[get_message_answer_service] = lambda: service
        client.app.dependency_overrides.update(overrides)
        return gen, cite

    _install()
    yield headers, conv_id, _install
    client.app.dependency_overrides.clear()


class TestSseWireFormat:
    def test_success_flow_emits_five_discriminated_events_and_persists_completed(
        self, client: TestClient, db_session: Session, sse_env
    ) -> None:
        headers, conv_id, install = sse_env
        generation, citations = install()
        with client.stream(
            "POST",
            f"/v1/conversations/{conv_id}/messages",
            json={"content": "什么是 X？"},
            headers=headers,
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            raw = b"".join(resp.iter_bytes())
        events = _parse_sse(raw)
        names = [name for name, _ in events]
        assert names == ["message_start", "retrieval_done", "delta", "delta", "message_end"]

        message_start = dict(events[0][1]["data"])
        message_id = uuid.UUID(message_start["message_id"])
        # retrieval_done 与引用详情复用同一 Citation 字段语义。
        citations_event = events[1][1]["data"]["citations"]
        assert len(citations_event) == 1
        citation = citations_event[0]
        for field in (
            "rank",
            "score",
            "chunk_id",
            "document_id",
            "document_version",
            "filename",
            "file_type",
            "page",
            "section",
            "content",
            "source_type",
        ):
            assert field in citation
        assert citation["source_type"] == "live"
        assert uuid.UUID(citation["chunk_id"])
        assert uuid.UUID(citation["document_id"])
        assert citation["filename"] == "source.txt"
        # delta 帧内容。
        assert events[2][1]["data"]["text"] == "根据"
        assert events[3][1]["data"]["text"] == "资料"
        # message_end 与正常终态。
        assert events[4][1]["data"]["message_id"] == str(message_id)
        assert events[4][1]["data"]["finish_reason"] == "stop"
        # 所有帧使用统一信封且 trace_id 一致。
        trace_ids = {frame["trace_id"] for _, frame in events}
        assert len(trace_ids) == 1
        for _, frame in events:
            assert frame["code"] == 0
            assert frame["msg"] == ""
        # 持久化：用户消息 completed、assistant completed/stop、引用已保存。
        messages = (
            db_session.query(Message)
            .filter_by(conversation_id=conv_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
        assert messages[0].status == MessageStatus.COMPLETED
        assert messages[1].id == message_id
        assert messages[1].status == MessageStatus.COMPLETED
        assert messages[1].finish_reason == MessageFinishReason.STOP
        assert messages[1].content == "根据资料"
        assert len(citations.saved) == 1
        assert citations.saved[0][0].rank == 1

    def test_no_evidence_flow_is_completed_stop_without_generation(
        self, client: TestClient, db_session: Session, sse_env
    ) -> None:
        headers, conv_id, install = sse_env
        generation, citations = install(
            retrieval=FakeRetrieval(
                result=RetrievalResult(query="q", candidates=(), context_pack="")
            )
        )
        with client.stream(
            "POST",
            f"/v1/conversations/{conv_id}/messages",
            json={"content": "无关问题"},
            headers=headers,
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            raw = b"".join(resp.iter_bytes())
        events = _parse_sse(raw)
        names = [name for name, _ in events]
        # 无证据：不发送 delta，直接结束。
        assert names == ["message_start", "retrieval_done", "message_end"]
        assert events[1][1]["data"]["citations"] == []
        assert events[2][1]["data"]["finish_reason"] == "stop"
        assert generation.calls == 0
        assert citations.saved == []
        messages = (
            db_session.query(Message)
            .filter_by(conversation_id=conv_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        assistant = messages[1]
        assert assistant.status == MessageStatus.COMPLETED
        assert assistant.finish_reason == MessageFinishReason.STOP
        assert assistant.content == NO_EVIDENCE_CONTENT

    def test_provider_error_emits_error_event_and_persists_failed_error(
        self, client: TestClient, db_session: Session, sse_env
    ) -> None:
        headers, conv_id, install = sse_env
        generation, citations = install(
            generation=FakeGeneration(error=GatewayError("provider_error", "provider failed"))
        )
        with client.stream(
            "POST",
            f"/v1/conversations/{conv_id}/messages",
            json={"content": "问题"},
            headers=headers,
        ) as resp:
            assert resp.status_code == 200
            raw = b"".join(resp.iter_bytes())
        events = _parse_sse(raw)
        names = [name for name, _ in events]
        assert names == ["message_start", "retrieval_done", "error"]
        error_frame = events[2][1]
        assert error_frame["code"] == 50000
        assert error_frame["msg"] == "系统繁忙，请稍后再试"
        assert error_frame["trace_id"]
        # 生成已调用（重试耗尽后失败），但失败路径不保存引用。
        assert generation.calls == 1
        assert citations.saved == []
        assistant = (
            db_session.query(Message)
            .filter_by(conversation_id=conv_id)
            .order_by(Message.created_at.desc())
            .first()
        )
        assert assistant is not None
        assert assistant.status == MessageStatus.FAILED
        assert assistant.finish_reason == MessageFinishReason.ERROR

    def test_no_completed_documents_returns_20005_json_not_sse(
        self, client: TestClient, sse_env
    ) -> None:
        headers, conv_id, install = sse_env
        install(retrieval=FakeRetrieval(retrievable=0))
        resp = client.post(
            f"/v1/conversations/{conv_id}/messages",
            json={"content": "问题"},
            headers=headers,
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == 20005
        assert body["msg"] == _KB_NOT_READY_MSG

    def test_cross_user_conversation_returns_20007_json(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        from app.models.conversation import Conversation
        from app.models.knowledge_base import KnowledgeBase
        from app.models.user import User

        other = User(email="sse-other@example.com", password_hash="x" * 60)
        db_session.add(other)
        db_session.flush()
        other_kb = KnowledgeBase(user_id=other.id, name="other")
        db_session.add(other_kb)
        db_session.flush()
        conv = Conversation(user_id=other.id, knowledge_base_id=other_kb.id)
        db_session.add(conv)
        db_session.commit()
        tokens = _register(client, "sse-intruder@example.com")
        resp = client.post(
            f"/v1/conversations/{conv.id}/messages",
            json={"content": "问题"},
            headers=_headers(tokens),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007


# 客户端断开（cancelled/cancelled）无法由 TestClient 模拟（流式响应在 ASGI 传输层
# 阻塞，不会取消应用任务；生产 uvicorn 在 send 时抛 ClientDisconnected 并关闭生成器）。
# 断开收敛通过集成测试直接 aclose() 异步生成器验证（test_sse_terminal_states.py）。
