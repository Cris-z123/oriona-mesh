"""资料级处理并发名额集成测试（T041 / FR-032、data-model.md 处理并发）。

覆盖：单用户最多 3 份资料同时 processing（名额来自数据库，非进程内计数）；
名额跨 parse/chunk/embed/finalize 持续持有并在完成时释放；失联 running 任务由
恢复扫描器原子关闭 attempt、释放名额并按重试预算恢复 queued 或收敛失败，且
不存在双活动 attempt。需要真实 PostgreSQL。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import get_settings
from app.infrastructure.storage.local import LocalStorage
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
)
from app.models.processing_lease import DocumentProcessingLease
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.services.llm.embeddings import EmbeddingService
from app.workers.document_chunk import process_chunk
from app.workers.document_embed import process_embed
from app.workers.document_finalize import process_finalize
from app.workers.document_parse import process_parse
from app.workers.task_recovery import scan_expired_leases

pytestmark = pytest.mark.integration

MAX_PER_USER = 3


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

    user = User(email="conc-owner@example.com", password_hash="x" * 60)
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


class FakeEmbeddings(EmbeddingService):
    """返回与文本数相同、维度 1536 的确定性向量。"""

    def __init__(self) -> None:
        # 测试替身不初始化模型网关。
        self.settings = get_settings()

    def embed_texts(self, texts: list[str], **kwargs) -> list[list[float]]:
        return [[0.1 + float(i) / 1000.0] + [0.0] * 1535 for i in range(len(texts))]


class TestProcessingSlots:
    def test_at_most_max_per_user_processing(
        self,
        db_session: Session,
        test_engine,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_ids = [
            _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, f"text {i}")
            for i in range(4)
        ]

        # 用独立会话逐个执行 parse worker，模拟并发名额竞争。
        factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
        processing: list[uuid.UUID] = []
        for doc_id in doc_ids:
            s = factory()
            try:
                task = s.query(DocumentTask).filter_by(document_id=doc_id, task_type="parse").one()
                process_parse(
                    s,
                    task_id=task.id,
                    user_id=user_id,
                    knowledge_base_id=kb_id,
                    document_id=doc_id,
                    document_version=1,
                    file_storage=storage,
                    dispatch=dispatch,
                )
                s.commit()
                fresh = s.get(Document, doc_id)
                if fresh is not None and fresh.status == DocumentStatus.PROCESSING:
                    processing.append(doc_id)
            finally:
                s.close()

        # 最多 3 份同时 processing；第 4 份保持 queued（可恢复等待）。
        assert len(processing) == MAX_PER_USER
        wait_doc = next(d for d in doc_ids if d not in processing)
        waiting = db_session.get(Document, wait_doc)
        assert waiting is not None
        assert waiting.status == DocumentStatus.QUEUED
        open_leases = (
            db_session.query(DocumentProcessingLease)
            .filter(
                DocumentProcessingLease.user_id == user_id,
                DocumentProcessingLease.released_at.is_(None),
            )
            .count()
        )
        assert open_leases == MAX_PER_USER

    def test_lease_held_across_stages_and_released_at_finalize(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(
            db_session, storage, dispatch, user_id, kb_id, "hello pipeline " * 50
        )
        calls.clear()  # 种子上传的投递不计入阶段切换断言

        task = db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="parse").one()
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
        chunk_task = (
            db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="chunk").one()
        )
        process_chunk(
            db_session,
            task_id=chunk_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        embed_task = (
            db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="embed").one()
        )
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
        finalize_task = (
            db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="finalize").one()
        )
        process_finalize(
            db_session,
            task_id=finalize_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            dispatch=dispatch,
        )

        # 全流水线只创建一条租约：跨阶段持有，最终释放。
        leases = db_session.query(DocumentProcessingLease).all()
        assert len(leases) == 1
        assert leases[0].released_at is not None
        assert leases[0].release_reason == "completed"
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.COMPLETED
        assert doc.current_task_type is None
        assert doc.chunk_count >= 1
        # 阶段切换投递链路：parse→chunk→embed→finalize。
        dispatched = [name for name, _ in calls]
        assert dispatched == [
            "orionamesh.document_chunk",
            "orionamesh.document_embed",
            "orionamesh.document_finalize",
        ]

    def test_lost_worker_recovered_to_queued_within_budget(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "lost worker")
        calls.clear()  # 种子上传的投递不计入恢复断言
        # 直接构造 running 状态：任务 running、attempt running、租约过期。
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        task = db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="parse").one()
        doc.status = DocumentStatus.PROCESSING
        task.status = DocumentTaskStatus.RUNNING
        task.retry_count = 0
        lease = DocumentProcessingLease(
            user_id=user_id,
            document_id=doc_id,
            task_id=task.id,
            acquired_at=datetime.now(UTC) - timedelta(seconds=600),
            heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
            expires_at=datetime.now(UTC) - timedelta(seconds=300),
        )
        db_session.add(lease)
        db_session.flush()
        attempt = DocumentTaskAttempt(
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            attempt_no=1,
            worker_name="lost-worker",
            status=DocumentAttemptStatus.RUNNING,
            started_at=datetime.now(UTC) - timedelta(seconds=600),
        )
        db_session.add(attempt)
        db_session.commit()

        recovered = scan_expired_leases(db_session, dispatch=dispatch, now=datetime.now(UTC))
        assert recovered == 1
        db_session.refresh(doc)
        db_session.refresh(task)
        db_session.refresh(lease)
        db_session.refresh(attempt)
        # 活动 attempt 关闭、名额释放、任务按预算恢复 queued 并重投。
        assert attempt.status == DocumentAttemptStatus.CANCELLED
        assert lease.released_at is not None
        assert task.status == DocumentTaskStatus.QUEUED
        assert task.retry_count == 1
        assert doc.status == DocumentStatus.QUEUED
        assert len(calls) == 1
        # 不存在双活动 attempt。
        open_attempts = (
            db_session.query(DocumentTaskAttempt)
            .filter_by(task_id=task.id, status=DocumentAttemptStatus.RUNNING)
            .count()
        )
        assert open_attempts == 0

    def test_retry_budget_exhausted_converges_failed(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "exhausted")
        calls.clear()  # 种子上传的投递不计入预算断言
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        task = db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="parse").one()
        doc.status = DocumentStatus.PROCESSING
        task.status = DocumentTaskStatus.RUNNING
        task.retry_count = task.max_retries  # 已达预算
        lease = DocumentProcessingLease(
            user_id=user_id,
            document_id=doc_id,
            task_id=task.id,
            acquired_at=datetime.now(UTC) - timedelta(seconds=600),
            heartbeat_at=datetime.now(UTC) - timedelta(seconds=600),
            expires_at=datetime.now(UTC) - timedelta(seconds=300),
        )
        db_session.add(lease)
        db_session.flush()
        db_session.add(
            DocumentTaskAttempt(
                task_id=task.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=1,
                attempt_no=4,
                worker_name="lost-worker",
                status=DocumentAttemptStatus.RUNNING,
                started_at=datetime.now(UTC) - timedelta(seconds=600),
            )
        )
        db_session.commit()

        recovered = scan_expired_leases(db_session, dispatch=dispatch, now=datetime.now(UTC))
        assert recovered == 1
        db_session.refresh(doc)
        db_session.refresh(task)
        # 达到预算不再排队：任务与资料收敛为明确失败（20014 无法归类的重试耗尽）。
        assert task.status == DocumentTaskStatus.FAILED
        assert task.error_code == 20014
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20014
        assert calls == []
        assert (
            db_session.query(DocumentTaskAttempt)
            .filter_by(status=DocumentAttemptStatus.RUNNING)
            .count()
            == 0
        )
