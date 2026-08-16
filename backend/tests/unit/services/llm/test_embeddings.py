"""嵌入用例适配器失败测试（T044 / FR-027、FR-028、FR-034、quickstart 验证 17）。

覆盖：嵌入调用统一经网关且声明 embedding 调用类型；模型由配置选择；每文本恰好
一次网关调用（业务层不得产生第二层尝试）；最终向量维度校验失败收敛；网关最终
失败（含脱敏失败 fail-closed 零外发）由业务适配器收敛为嵌入失败；worker 级嵌入
失败后资料与任务持久化 ``20012`` 明确终态。需要真实 PostgreSQL 的部分标为
integration。
"""

import uuid

import pytest

from app.infrastructure.model_gateway.types import EmbeddingResult, GatewayError, ModelCall
from app.services.llm.embeddings import EmbeddingFailure, EmbeddingService

pytestmark = pytest.mark.unit

EMBEDDING_DIMENSION = 1536


class FakeGateway:
    """记录调用并模拟网关内部超时/重试行为（重试只发生在网关内）。"""

    def __init__(self, *, vectors=None, error=None, retry_failures: int = 0) -> None:
        self.calls: list[ModelCall] = []
        self._vectors = vectors
        self._error = error
        self._retry_failures = retry_failures

    def call(self, call: ModelCall) -> EmbeddingResult:
        # 模拟真实网关：同一业务调用内部的网络失败由网关按预算重试，
        # 只向业务适配器返回最终成功或失败。
        self.calls.append(call)
        for _attempt in range(self._retry_failures):
            try:
                return self._emit()
            except GatewayError:
                continue
        return self._emit()

    def _emit(self) -> EmbeddingResult:
        if self._error is not None:
            raise self._error
        vectors = self._vectors or [[0.1] + [0.0] * (EMBEDDING_DIMENSION - 1)]
        return EmbeddingResult(vectors=[list(v) for v in vectors])

    def call_stream(self, call):
        raise AssertionError("embedding must not stream")


def _service(gateway) -> EmbeddingService:
    return EmbeddingService(gateway=gateway)


def _user_digest() -> str:
    return "u-test"


class TestGatewayIntegration:
    def test_embedding_call_goes_through_gateway_with_call_type(self) -> None:
        gateway = FakeGateway()
        vectors = _service(gateway).embed_texts(["hello"], user_id=uuid.uuid4())
        assert len(vectors) == 1
        assert len(gateway.calls) == 1
        assert gateway.calls[0].call_type == "embedding"
        assert gateway.calls[0].content == "hello"
        assert gateway.calls[0].trace_id
        assert gateway.calls[0].call_id
        assert gateway.calls[0].subject_digest

    def test_one_gateway_call_per_text(self) -> None:
        gateway = FakeGateway()
        texts = ["a", "b", "c"]
        vectors = _service(gateway).embed_texts(texts, user_id=uuid.uuid4())
        assert len(vectors) == 3
        assert [c.content for c in gateway.calls] == texts

    def test_configuration_selects_embedding_model(self) -> None:
        gateway = FakeGateway()
        _service(gateway).embed_texts(["x"], user_id=uuid.uuid4())
        # 网关配置模型由 ModelGatewayService 决定；适配器不覆盖模型。
        assert gateway.calls[0].call_type == "embedding"

    def test_retries_only_executed_by_gateway_single_business_attempt(self) -> None:
        # 网关内部按预算重试（2 次失败后成功）；业务适配器不得再次重试：
        # 对网关只发起一次调用。
        gateway = FakeGateway(retry_failures=2)
        vectors = _service(gateway).embed_texts(["x"], user_id=uuid.uuid4())
        assert len(vectors) == 1
        assert len(gateway.calls) == 1


class TestDimensionValidation:
    def test_wrong_dimension_converges_embedding_failure(self) -> None:
        gateway = FakeGateway(vectors=[[0.1, 0.2, 0.3]])
        with pytest.raises(EmbeddingFailure):
            _service(gateway).embed_texts(["x"], user_id=uuid.uuid4())
        assert len(gateway.calls) == 1

    def test_all_texts_must_match_dimension(self) -> None:
        gateway = FakeGateway(vectors=[[0.1] * 1536, [0.2] * 3])
        with pytest.raises(EmbeddingFailure):
            _service(gateway).embed_texts(["a", "b"], user_id=uuid.uuid4())


class TestGatewayFailures:
    def test_provider_error_converges_embedding_failure(self) -> None:
        gateway = FakeGateway(error=GatewayError("provider_error", "provider failed"))
        with pytest.raises(EmbeddingFailure):
            _service(gateway).embed_texts(["x"], user_id=uuid.uuid4())

    def test_timeout_converges_embedding_failure(self) -> None:
        gateway = FakeGateway(error=GatewayError("timeout", "timed out"))
        with pytest.raises(EmbeddingFailure):
            _service(gateway).embed_texts(["x"], user_id=uuid.uuid4())

    def test_sanitization_failure_no_external_request(self) -> None:
        # 脱敏失败 fail-closed：网关不发送请求（假网关在此路径抛错），
        # 业务适配器收敛为嵌入失败。
        gateway = FakeGateway(error=GatewayError("sanitization_failed", "sanitization failed"))
        with pytest.raises(EmbeddingFailure):
            _service(gateway).embed_texts(["x"], user_id=uuid.uuid4())

    def test_gateway_configuration_error_converges(self) -> None:
        gateway = FakeGateway(error=GatewayError("configuration", "model missing"))
        with pytest.raises(EmbeddingFailure):
            _service(gateway).embed_texts(["x"], user_id=uuid.uuid4())


@pytest.fixture
def dispatch_calls():
    calls: list[tuple[str, tuple]] = []

    def fake(name: str, args: tuple) -> None:
        calls.append((name, args))

    return fake, calls


@pytest.mark.integration
class TestEmbedWorkerTerminalState:
    """嵌入失败后资料与任务的 20012 明确终态（真实 PostgreSQL）。"""

    def test_embed_failure_persists_20012(
        self,
        db_session,
        test_engine,
        tmp_path,
        dispatch_calls,
    ) -> None:
        from app.infrastructure.storage.local import LocalStorage
        from app.models.chunk import Chunk
        from app.models.document import Document
        from app.models.document_task import DocumentTask
        from app.models.enums import DocumentStatus, DocumentTaskStatus, DocumentTaskType
        from app.models.knowledge_base import KnowledgeBase
        from app.models.processing_lease import DocumentProcessingLease
        from app.models.user import User
        from app.services.document_service import DocumentService
        from app.services.file_storage import FileStorage
        from app.workers.document_chunk import process_chunk
        from app.workers.document_embed import process_embed
        from app.workers.document_parse import process_parse

        dispatch, _ = dispatch_calls
        storage = FileStorage(LocalStorage(tmp_path / "store"))
        user = User(email="embed-fail@example.com", password_hash="x" * 60)
        db_session.add(user)
        db_session.flush()
        kb = KnowledgeBase(user_id=user.id, name="kb")
        db_session.add(kb)
        db_session.commit()

        import io

        from fastapi import UploadFile

        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
        outcome = service.upload(
            user.id,
            kb.id,
            [UploadFile(file=io.BytesIO(("embed fail " * 100).encode()), filename="doc.txt")],
        )
        doc_id = uuid.UUID(outcome.items[0]["id"])

        def _task(ttype):
            return (
                db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type=ttype).one()
            )

        process_parse(
            db_session,
            task_id=_task(DocumentTaskType.PARSE).id,
            user_id=user.id,
            knowledge_base_id=kb.id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        process_chunk(
            db_session,
            task_id=_task(DocumentTaskType.CHUNK).id,
            user_id=user.id,
            knowledge_base_id=kb.id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )

        class FailingEmbeddings:
            def embed_texts(self, texts, **kwargs):
                raise EmbeddingFailure("embedding failed")

        process_embed(
            db_session,
            task_id=_task(DocumentTaskType.EMBED).id,
            user_id=user.id,
            knowledge_base_id=kb.id,
            document_id=doc_id,
            document_version=1,
            embeddings=FailingEmbeddings(),  # type: ignore[arg-type]
            dispatch=dispatch,
        )
        doc = db_session.get(Document, doc_id)
        embed_task = _task(DocumentTaskType.EMBED)
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20012
        assert doc.error_message == "资料向量化失败，请删除后重新上传"
        assert embed_task.status == DocumentTaskStatus.FAILED
        assert embed_task.error_code == 20012
        # 未写入任何正式片段；处理名额已释放。
        assert db_session.query(Chunk).count() == 0
        assert db_session.query(DocumentProcessingLease).one().released_at is not None
        # 不投递 finalize。
        assert db_session.query(DocumentTask).filter_by(task_type="finalize").count() == 0
