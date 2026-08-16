"""流水线事务编排与写入 fencing 集成测试（T042 / FR-007、FR-009、data-model.md
阶段切换与持久化写入边界）。

覆盖：attempt/task 成功、下一阶段幂等创建、``current_task_type`` 与 ``lease.task_id``
同事务一致、提交后才投递（投递失败由扫描器重投，DB 真相不丢失）；解析结果/草稿/
正式 chunks 写入均携带 ``attempt_id`` 并同事务校验 running、版本一致、资料非
deleting；embed 直写正式 chunks；finalize 只校验数量/版本并翻转 completed；发布前
不可检索；数量不一致持久化 ``20013``。需要真实 PostgreSQL。
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.infrastructure.storage.local import LocalStorage
from app.models.chunk import Chunk, DocumentChunkDraft, DocumentParseResult
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
)
from app.models.processing_lease import DocumentProcessingLease
from app.repositories.chunk_drafts import ChunkDraftRepository
from app.repositories.chunks import ChunkRepository
from app.repositories.fencing import FencingError
from app.repositories.parse_results import ParseResultRepository
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.services.llm.embeddings import EmbeddingService
from app.workers.base import begin_attempt
from app.workers.document_chunk import process_chunk
from app.workers.document_embed import process_embed
from app.workers.document_finalize import process_finalize
from app.workers.document_parse import process_parse

pytestmark = pytest.mark.integration

_FINALIZE_FAILED_MSG = "资料处理结果不一致，请删除后重新上传"


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    return FileStorage(LocalStorage(tmp_path / "store"))


@pytest.fixture
def dispatch_calls():
    calls: list[tuple[str, tuple]] = []

    def fake(name: str, args: tuple) -> None:
        calls.append((name, args))

    return fake, calls


@pytest.fixture
def user_and_kb(db_session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User

    user = User(email="pipe-owner@example.com", password_hash="x" * 60)
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db_session.add(kb)
    db_session.commit()
    return user.id, kb.id


def _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, text: str) -> uuid.UUID:
    import io

    from fastapi import UploadFile

    service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
    outcome = service.upload(
        user_id, kb_id, [UploadFile(file=io.BytesIO(text.encode()), filename="doc.txt")]
    )
    return uuid.UUID(outcome.items[0]["id"])


def _task(db_session, doc_id, task_type) -> DocumentTask:
    return db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type=task_type).one()


class FakeEmbeddings(EmbeddingService):
    def __init__(self) -> None:
        # 测试替身不初始化模型网关。
        self.settings = get_settings()

    def embed_texts(self, texts: list[str], **kwargs) -> list[list[float]]:
        return [[0.1] + [0.0] * 1535 for _ in texts]


class TestStageOrchestration:
    def test_stage_completion_consistent_commit_then_dispatch(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "stage txn")
        calls.clear()  # 种子上传的投递不计入断言

        task = _task(db_session, doc_id, DocumentTaskType.PARSE)
        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.refresh(task)
        # 当前 attempt/task 成功并携带完整终态字段。
        attempt = db_session.query(DocumentTaskAttempt).one()
        assert attempt.status == DocumentAttemptStatus.SUCCEEDED
        assert attempt.finished_at is not None
        assert attempt.duration_ms is not None and attempt.duration_ms >= 0
        assert task.status == DocumentTaskStatus.SUCCEEDED
        # 下一阶段幂等创建并排队；current_task_type 与 lease.task_id 同事务一致。
        chunk_task = _task(db_session, doc_id, DocumentTaskType.CHUNK)
        assert chunk_task.status == DocumentTaskStatus.QUEUED
        assert chunk_task.retry_count == 0
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.current_task_type == DocumentTaskType.CHUNK
        assert doc.retry_count == 0
        lease = db_session.query(DocumentProcessingLease).one()
        assert lease.task_id == chunk_task.id
        # 提交后才投递。
        assert calls == [("orionamesh.document_chunk", (chunk_task.id,))]

    def test_dispatch_failure_after_commit_keeps_queued_truth(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "dispatch x")
        task = _task(db_session, doc_id, DocumentTaskType.PARSE)

        def boom(*_args):
            raise RuntimeError("celery broker down")

        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=boom,
        )
        # DB 真相不因投递失败而丢失：当前任务已成功、下一阶段保持 queued。
        parse_task = _task(db_session, doc_id, DocumentTaskType.PARSE)
        assert parse_task.status == DocumentTaskStatus.SUCCEEDED
        chunk_task = _task(db_session, doc_id, DocumentTaskType.CHUNK)
        assert chunk_task.status == DocumentTaskStatus.QUEUED

    def test_stage_completion_idempotent_replay(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "replay")
        calls.clear()  # 种子上传的投递不计入断言
        task = _task(db_session, doc_id, DocumentTaskType.PARSE)
        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        # 重复投递/重放 complete_stage：不创建第二个下一阶段任务，仅重投。
        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        chunk_tasks = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type=DocumentTaskType.CHUNK)
            .all()
        )
        assert len(chunk_tasks) == 1


class TestWriteFencing:
    def test_parse_result_write_fenced_to_attempt(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "fence")
        task = _task(db_session, doc_id, DocumentTaskType.PARSE)
        started_task, attempt = begin_attempt(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            worker_name="test",
        )
        db_session.commit()
        repo = ParseResultRepository(db_session)
        result = repo.save(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            content_object_key="parse/x",
            content_hash="h",
            parser_name="txt",
            parser_version="1",
            normalized_chars=4,
        )
        db_session.commit()
        assert result.id is not None
        assert db_session.query(DocumentParseResult).count() == 1

    def test_write_rejected_when_document_deleting(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "fence-del")
        task = _task(db_session, doc_id, DocumentTaskType.PARSE)
        started_task, attempt = begin_attempt(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            worker_name="test",
        )
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        doc.status = DocumentStatus.DELETING
        db_session.commit()
        with pytest.raises(FencingError):
            ParseResultRepository(db_session).save(
                attempt_id=attempt.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=1,
                content_object_key="parse/x",
                content_hash="h",
                parser_name="txt",
                parser_version="1",
                normalized_chars=4,
            )
        db_session.rollback()
        assert db_session.query(DocumentParseResult).count() == 0

    def test_write_rejected_on_version_mismatch(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "fence-v")
        task = _task(db_session, doc_id, DocumentTaskType.PARSE)
        started_task, attempt = begin_attempt(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            worker_name="test",
        )
        db_session.commit()
        with pytest.raises(FencingError):
            ParseResultRepository(db_session).save(
                attempt_id=attempt.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=2,  # 与资料当前版本不一致
                content_object_key="parse/x",
                content_hash="h",
                parser_name="txt",
                parser_version="1",
                normalized_chars=4,
            )
        db_session.rollback()
        assert db_session.query(DocumentParseResult).count() == 0

    def test_write_rejected_when_attempt_no_longer_running(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "fence-a")
        task = _task(db_session, doc_id, DocumentTaskType.PARSE)
        started_task, attempt = begin_attempt(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            worker_name="test",
        )
        attempt.status = DocumentAttemptStatus.SUCCEEDED
        attempt.finished_at = datetime.now(UTC)
        db_session.commit()
        with pytest.raises(FencingError):
            ParseResultRepository(db_session).save(
                attempt_id=attempt.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=1,
                content_object_key="parse/x",
                content_hash="h",
                parser_name="txt",
                parser_version="1",
                normalized_chars=4,
            )
        db_session.rollback()
        assert db_session.query(DocumentParseResult).count() == 0

    def test_draft_and_chunk_writes_fenced(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "fence-c")
        task = _task(db_session, doc_id, DocumentTaskType.PARSE)
        started_task, attempt = begin_attempt(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            worker_name="test",
        )
        db_session.commit()
        draft = DocumentChunkDraft(
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            seq=0,
            content="draft",
        )
        ChunkDraftRepository(db_session).replace_for_version(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            drafts=[draft],
        )
        db_session.commit()
        assert db_session.query(DocumentChunkDraft).count() == 1
        # 资料 deleting 后草稿写入被拒绝。
        hidden = db_session.get(Document, doc_id)
        assert hidden is not None
        hidden.status = DocumentStatus.DELETING
        db_session.commit()
        with pytest.raises(FencingError):
            ChunkDraftRepository(db_session).replace_for_version(
                attempt_id=attempt.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=1,
                drafts=[draft],
            )
        db_session.rollback()


class TestEmbedAndFinalize:
    def test_embed_writes_chunks_directly_and_finalize_publishes(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(
            db_session, storage, dispatch, user_id, kb_id, "publish me " * 50
        )
        process_parse(
            db_session,
            task_id=_task(db_session, doc_id, DocumentTaskType.PARSE).id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        process_chunk(
            db_session,
            task_id=_task(db_session, doc_id, DocumentTaskType.CHUNK).id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        embed_task = _task(db_session, doc_id, DocumentTaskType.EMBED)
        process_embed(
            db_session,
            task_id=embed_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            embeddings=FakeEmbeddings(),
            dispatch=dispatch,
        )
        # embed 直写正式 chunks（幂等逻辑键），但 finalize 前不可检索。
        chunks = db_session.query(Chunk).all()
        assert len(chunks) >= 1
        assert all(c.document_version == 1 and c.user_id == user_id for c in chunks)
        retrievable = ChunkRepository(db_session).count_retrievable(user_id, kb_id)
        assert retrievable == 0

        finalize_task = _task(db_session, doc_id, DocumentTaskType.FINALIZE)
        process_finalize(
            db_session,
            task_id=finalize_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            dispatch=dispatch,
        )
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.COMPLETED
        assert doc.chunk_count == len(chunks)
        assert doc.current_task_type is None
        assert ChunkRepository(db_session).count_retrievable(user_id, kb_id) == len(chunks)

    def test_multi_batch_embed_keeps_all_chunks(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        """C2 回归：超过 EMBED_BATCH_SIZE（32）的草稿分多批嵌入后不得互相覆盖。"""
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        # 33+ 个 800 字符草稿（约 26KB 文本）。
        doc_id = _seed_queued_document(
            db_session, storage, dispatch, user_id, kb_id, "多批嵌入 " * 6_000
        )
        process_parse(
            db_session,
            task_id=_task(db_session, doc_id, DocumentTaskType.PARSE).id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        process_chunk(
            db_session,
            task_id=_task(db_session, doc_id, DocumentTaskType.CHUNK).id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        embed_task = _task(db_session, doc_id, DocumentTaskType.EMBED)
        process_embed(
            db_session,
            task_id=embed_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            embeddings=FakeEmbeddings(),
            dispatch=dispatch,
        )
        drafts = db_session.query(DocumentChunkDraft).count()
        chunks = db_session.query(Chunk).count()
        assert drafts >= 33  # 确保超过单批大小
        assert chunks == drafts  # 全部批次存活，无覆盖
        process_finalize(
            db_session,
            task_id=_task(db_session, doc_id, DocumentTaskType.FINALIZE).id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            dispatch=dispatch,
        )
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.COMPLETED
        assert doc.chunk_count == drafts

    def test_finalize_count_mismatch_20013(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(
            db_session, storage, dispatch, user_id, kb_id, "mismatch " * 50
        )
        calls.clear()  # 种子上传的投递不计入断言
        process_parse(
            db_session,
            task_id=_task(db_session, doc_id, DocumentTaskType.PARSE).id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        process_chunk(
            db_session,
            task_id=_task(db_session, doc_id, DocumentTaskType.CHUNK).id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        embed_task = _task(db_session, doc_id, DocumentTaskType.EMBED)
        process_embed(
            db_session,
            task_id=embed_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            embeddings=FakeEmbeddings(),
            dispatch=dispatch,
        )
        # 模拟发布前数据缺失：正式片段数量与任务结果不一致。
        db_session.query(Chunk).delete()
        db_session.commit()
        finalize_task = _task(db_session, doc_id, DocumentTaskType.FINALIZE)
        process_finalize(
            db_session,
            task_id=finalize_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            dispatch=dispatch,
        )
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20013
        assert doc.error_message == _FINALIZE_FAILED_MSG
        assert finalize_task.status == DocumentTaskStatus.FAILED
        assert finalize_task.error_code == 20013
        # 发布失败：未发布片段仍不可检索；lease 已释放。
        assert ChunkRepository(db_session).count_retrievable(user_id, kb_id) == 0
        assert db_session.query(DocumentProcessingLease).one().released_at is not None
        # 发布失败不产生新的 finalize 投递（仅 embed 阶段交接的一次）。
        finalize_dispatches = [name for name, _ in calls if name == "orionamesh.document_finalize"]
        assert len(finalize_dispatches) == 1
