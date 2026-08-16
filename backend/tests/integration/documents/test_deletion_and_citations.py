"""资料删除与引用快照集成测试（T079 / FR-011、FR-016、FR-017、FR-020）。

覆盖：首次 DELETE 立即隐藏并递增 ``delete_cycle``/新建 ``delete_cleanup`` 任务；
``deleting`` 重复 DELETE 幂等且轮次/任务数不变；``failed/delete_cleanup/20015``
重试才递增轮次/新建任务且旧历史不可修改；``deleted`` 后 DELETE/GET 404；
无运行 attempt 立即接管；运行写入被 fencing 拒绝；等待不超过
``lease.expires_at``；租约超时扫描 cancelled/释放/激活清理；清理失败重试耗尽
最小墓碑与 ``retry_delete``；删除后检索排除；历史引用保留必填快照
（``source_type=snapshot``）。需要真实 PostgreSQL 与 Redis。
"""

import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.conversation import Conversation, Message, MessageCitation
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    MessageFinishReason,
    MessageRole,
    MessageStatus,
)
from app.models.processing_lease import DocumentProcessingLease
from app.services.citation_service import CitationService
from app.services.document_deletion_service import DocumentDeletionService
from app.services.document_pipeline import DocumentPipelineOrchestrator
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.workers.document_delete_cleanup import process_delete_cleanup
from app.workers.task_recovery import scan_expired_leases

pytestmark = pytest.mark.integration

_RESOURCE_NOT_FOUND_MSG = "请求的资源不存在"


def _uf(name: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name)


def _seed_document(
    db_session: Session, storage: FileStorage, dispatch, user_id: uuid.UUID, kb_id: uuid.UUID
) -> uuid.UUID:
    service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
    outcome = service.upload(user_id, kb_id, [_uf("doc.txt", b"hello deletion")])
    return uuid.UUID(outcome.items[0]["id"])


def _cleanup_task(
    db_session: Session, doc_id: uuid.UUID, delete_cycle: int | None = None
) -> DocumentTask:
    """取删除清理任务；默认取最新轮次（重试后会存在多个历史清理任务）。"""
    query = db_session.query(DocumentTask).filter_by(
        document_id=doc_id, task_type=DocumentTaskType.DELETE_CLEANUP
    )
    if delete_cycle is not None:
        query = query.filter_by(delete_cycle=delete_cycle)
    else:
        query = query.order_by(DocumentTask.delete_cycle.desc())
    task = query.first()
    assert task is not None
    return task


def _running_state(
    db_session: Session,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    *,
    lease_expired: bool,
) -> tuple[DocumentTask, DocumentTaskAttempt, DocumentProcessingLease]:
    """资料 processing、任务/attempt running、持有处理租约的失联前状态。"""
    doc = db_session.get(Document, doc_id)
    assert doc is not None
    task = db_session.query(DocumentTask).filter_by(document_id=doc_id).one()
    task.status = DocumentTaskStatus.RUNNING
    doc.status = DocumentStatus.PROCESSING
    doc.current_task_type = task.task_type
    started = datetime.now(UTC) - timedelta(seconds=600)
    lease = DocumentProcessingLease(
        user_id=user_id,
        document_id=doc_id,
        task_id=task.id,
        acquired_at=started,
        heartbeat_at=started,
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=300)
            if lease_expired
            else datetime.now(UTC) + timedelta(seconds=300)
        ),
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
        started_at=started,
    )
    db_session.add(attempt)
    db_session.commit()
    return task, attempt, lease


class TestDocumentDeleteLifecycle:
    """首次删除/幂等重放/失败重试/墓碑终态。"""

    def test_first_delete_hides_increments_cycle_and_creates_task(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_document(db_session, storage, dispatch, user_id, kb_id)
        calls.clear()  # 种子上传的投递不计入断言
        before_tasks = db_session.query(DocumentTask).count()

        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        # 首次删除：递增轮次、置 deleting、新建 delete_cleanup（deleting 立即隐藏）。
        assert doc.status == DocumentStatus.DELETING
        assert doc.delete_cycle == 1
        assert doc.current_task_type == DocumentTaskType.DELETE_CLEANUP
        assert doc.retry_count == 0
        cleanup = _cleanup_task(db_session, doc_id)
        assert cleanup.status == DocumentTaskStatus.QUEUED
        assert cleanup.delete_cycle == 1
        assert db_session.query(DocumentTask).count() == before_tasks + 1
        # 无运行 attempt：立即接管并投递清理。
        assert calls == [("orionamesh.document_delete_cleanup", (cleanup.id,))]

    def test_repeat_delete_deleting_idempotent_no_new_tasks(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_document(db_session, storage, dispatch, user_id, kb_id)
        service = DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch)
        service.delete(user_id, kb_id, doc_id)
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        tasks_before = db_session.query(DocumentTask).count()
        calls.clear()
        service.delete(user_id, kb_id, doc_id)  # 重复 DELETE 幂等 200
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETING
        assert doc.delete_cycle == 1  # 不递增轮次
        assert db_session.query(DocumentTask).count() == tasks_before  # 不创建任务
        assert calls == []  # 不重复投递

    def test_cleanup_success_leaves_deleted_tombstone(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_document(db_session, storage, dispatch, user_id, kb_id)
        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        cleanup = _cleanup_task(db_session, doc_id)
        process_delete_cleanup(
            db_session,
            task_id=cleanup.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETED  # 不可查询墓碑
        assert doc.current_task_type is None
        assert doc.error_code is None
        db_session.refresh(cleanup)
        assert cleanup.status == DocumentTaskStatus.SUCCEEDED
        # 原始对象已清理。
        assert not storage.storage.has_final(doc.upload_batch_id, doc_id)
        assert not storage.storage.has_temp(doc.upload_batch_id, doc_id)

    def test_delete_cleanup_failed_20015_retry_increments_cycle_keeps_history(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_document(db_session, storage, dispatch, user_id, kb_id)
        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        calls.clear()

        class FailingStorage(FileStorage):
            def delete_object(self, object_key: str) -> None:
                raise OSError("disk gone")

        broken = FailingStorage(storage.storage)
        cleanup = _cleanup_task(db_session, doc_id)
        for _round in range(4):  # 初次 + 3 次重试全部失败 → 20015 墓碑
            process_delete_cleanup(
                db_session,
                task_id=cleanup.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=1,
                file_storage=broken,
                dispatch=dispatch,
            )
            db_session.expire_all()
            cleanup = _cleanup_task(db_session, doc_id)
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.FAILED
        assert doc.current_task_type == DocumentTaskType.DELETE_CLEANUP
        assert doc.error_code == 20015
        assert doc.error_message == "资料删除未完成，请重试删除"
        assert cleanup.status == DocumentTaskStatus.FAILED
        assert cleanup.retry_count == 3
        old_attempts = (
            db_session.query(DocumentTaskAttempt)
            .filter_by(task_id=cleanup.id)
            .order_by(DocumentTaskAttempt.attempt_no)
            .all()
        )
        assert [a.attempt_no for a in old_attempts] == [1, 2, 3, 4]
        assert all(a.status == DocumentAttemptStatus.FAILED for a in old_attempts)

        # 从 failed/delete_cleanup/20015 重试：递增轮次、新建任务，旧历史不可修改。
        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.delete_cycle == 2
        old_cleanup = _cleanup_task(db_session, doc_id, delete_cycle=1)
        new_cleanup = _cleanup_task(db_session, doc_id)
        assert new_cleanup.id != old_cleanup.id  # 新任务，而非复用旧任务
        assert new_cleanup.delete_cycle == 2
        assert new_cleanup.status == DocumentTaskStatus.QUEUED
        assert new_cleanup.retry_count == 0
        # 旧任务/attempt/retry_count 不可修改。
        db_session.refresh(old_cleanup)
        assert old_cleanup.status == DocumentTaskStatus.FAILED
        assert old_cleanup.error_code == 20015
        assert old_cleanup.retry_count == 3
        assert [a.attempt_no for a in old_attempts] == [1, 2, 3, 4]
        # 投递：前 3 次为预算内清理重试（旧任务），最后一次为重试删除的新任务。
        assert calls[:3] == [("orionamesh.document_delete_cleanup", (old_cleanup.id,))] * 3
        assert calls[-1] == ("orionamesh.document_delete_cleanup", (new_cleanup.id,))

        # 重试成功：收敛为 deleted 墓碑。
        process_delete_cleanup(
            db_session,
            task_id=new_cleanup.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETED


class TestBoundedDeletionTakeover:
    """运行 attempt 的有界等待与扫描器接管。"""

    def test_running_attempt_with_lease_waits_until_expiry(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_document(db_session, storage, dispatch, user_id, kb_id)
        calls.clear()
        task, attempt, lease = _running_state(
            db_session, user_id, kb_id, doc_id, lease_expired=False
        )
        frozen_expiry = lease.expires_at

        # 有活动 attempt + 租约：不提前释放、不无限等待、不立即投递。
        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETING
        assert doc.delete_cycle == 1
        lease = db_session.get(DocumentProcessingLease, lease.id)
        assert lease is not None
        assert lease.released_at is None  # 保留租约（冻结等待上限）
        assert lease.expires_at == frozen_expiry  # 心跳不得延长
        assert attempt.status == DocumentAttemptStatus.RUNNING
        cleanup = _cleanup_task(db_session, doc_id)
        assert cleanup.status == DocumentTaskStatus.PENDING  # 等待扫描器接管后激活
        assert calls == []  # 未投递

        # 租约到期后：扫描器取消 attempt/task、释放名额并激活清理。
        lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()
        recovered = scan_expired_leases(db_session, dispatch=dispatch, now=datetime.now(UTC))
        assert recovered == 1
        db_session.refresh(attempt)
        db_session.refresh(task)
        db_session.refresh(lease)
        db_session.refresh(cleanup)
        assert attempt.status == DocumentAttemptStatus.CANCELLED
        assert task.status == DocumentTaskStatus.CANCELLED
        assert lease.released_at is not None
        assert cleanup.status == DocumentTaskStatus.QUEUED
        assert calls == [("orionamesh.document_delete_cleanup", (cleanup.id,))]

    def test_running_attempt_without_lease_immediate_takeover(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_document(db_session, storage, dispatch, user_id, kb_id)
        calls.clear()
        task, attempt, _ = _running_state(db_session, user_id, kb_id, doc_id, lease_expired=False)
        # 运行中的 attempt 无活动租约：视为失联，删除事务立即接管。
        from app.models.processing_lease import DocumentProcessingLease

        for lease in db_session.query(DocumentProcessingLease).all():
            db_session.delete(lease)
        db_session.commit()

        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        db_session.refresh(attempt)
        db_session.refresh(task)
        assert attempt.status == DocumentAttemptStatus.CANCELLED
        assert task.status == DocumentTaskStatus.CANCELLED
        cleanup = _cleanup_task(db_session, doc_id)
        assert cleanup.status == DocumentTaskStatus.QUEUED
        assert calls == [("orionamesh.document_delete_cleanup", (cleanup.id,))]

    def test_fencing_rejects_write_after_delete_committed(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        from app.repositories.fencing import FencingError

        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_document(db_session, storage, dispatch, user_id, kb_id)
        task, attempt, _ = _running_state(db_session, user_id, kb_id, doc_id, lease_expired=False)
        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        db_session.expire_all()

        # 删除提交后，旧 attempt 的持久化写入被 fencing 拒绝（worker 不得绕过重试）。
        with pytest.raises(FencingError):
            DocumentPipelineOrchestrator(db_session, dispatch=dispatch).complete_stage(
                attempt_id=attempt.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=1,
            )
        db_session.rollback()
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETING
        attempt = db_session.get(DocumentTaskAttempt, attempt.id)
        assert attempt is not None
        assert attempt.status == DocumentAttemptStatus.RUNNING  # 等待有界接管，未被篡改


class TestRetrievalAndCitations:
    """删除后检索排除与历史引用快照。"""

    def _completed_doc_with_chunks(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch,
        user_id: uuid.UUID,
        kb_id: uuid.UUID,
    ) -> uuid.UUID:
        doc_id = _seed_document(db_session, storage, dispatch, user_id, kb_id)
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        doc.status = DocumentStatus.COMPLETED
        word = "uniquetokenablephrase"
        for seq in (1, 2):
            db_session.add(
                Chunk(
                    user_id=user_id,
                    knowledge_base_id=kb_id,
                    document_id=doc_id,
                    document_version=1,
                    seq=seq,
                    content=f"{word} {word} {word}",
                    embedding=[0.0] * 1536,
                    embedding_model="text-embedding-3-small",
                    policy_version="v1",
                    page=1,
                    section="s",
                )
            )
        db_session.commit()
        return doc_id

    def test_retrieval_excludes_deleted_document(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        from app.repositories.chunks import ChunkRepository

        repo = ChunkRepository(db_session)
        doc_id = self._completed_doc_with_chunks(db_session, storage, dispatch, user_id, kb_id)
        # 删除前：双路检索均可命中已完成资料的片段。
        vector_hits = repo.vector_search(user_id, kb_id, [0.0] * 1536, min_similarity=0.65)
        keyword_hits = repo.keyword_search(
            user_id, kb_id, "uniquetokenablephrase", min_similarity=0.30
        )
        assert len(vector_hits) == 2
        assert len(keyword_hits) >= 1
        assert repo.count_retrievable(user_id, kb_id) == 2

        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        cleanup = _cleanup_task(db_session, doc_id)
        process_delete_cleanup(
            db_session,
            task_id=cleanup.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        # 删除后：片段与派生数据清除，检索排除该资料。
        assert repo.vector_search(user_id, kb_id, [0.0] * 1536, min_similarity=0.65) == []
        assert (
            repo.keyword_search(user_id, kb_id, "uniquetokenablephrase", min_similarity=0.30) == []
        )
        assert repo.count_retrievable(user_id, kb_id) == 0

    def test_citations_keep_snapshot_after_delete(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = self._completed_doc_with_chunks(db_session, storage, dispatch, user_id, kb_id)
        chunk = db_session.query(Chunk).filter_by(document_id=doc_id).first()
        assert chunk is not None

        conversation = Conversation(user_id=user_id, knowledge_base_id=kb_id, title="历史")
        db_session.add(conversation)
        db_session.flush()
        message = Message(
            user_id=user_id,
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            finish_reason=MessageFinishReason.STOP,
            content="回答",
        )
        db_session.add(message)
        db_session.flush()
        snapshot = {
            "filename": "doc.txt",
            "file_type": "txt",
            "page": 1,
            "section": "s",
            "content": "uniquetokenablephrase",
        }
        db_session.add(
            MessageCitation(
                message_id=message.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                chunk_id=chunk.id,
                document_id=doc_id,
                document_version=1,
                rank=1,
                score=0.9,
                chunk_snapshot=snapshot,
            )
        )
        db_session.commit()

        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, kb_id, doc_id
        )
        cleanup = _cleanup_task(db_session, doc_id)
        process_delete_cleanup(
            db_session,
            task_id=cleanup.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.expire_all()

        # 历史引用保留：外键置空、快照必填；消息终态不受影响。
        citation = db_session.query(MessageCitation).one()
        assert citation.chunk_id is None
        assert citation.document_id is None
        assert citation.chunk_snapshot == snapshot
        message = db_session.get(Message, message.id)
        assert message is not None
        assert message.status == MessageStatus.COMPLETED
        assert message.finish_reason == MessageFinishReason.STOP

        rows, total = CitationService(db_session).list_for_message(
            message.id, conversation.id, user_id, page=1, page_size=20
        )
        assert total == 1
        dto = rows[0]
        assert dto["source_type"] == "snapshot"
        assert dto["chunk_id"] is None
        assert dto["document_id"] is None
        assert dto["filename"] == "doc.txt"
        assert dto["file_type"] == "txt"
        assert dto["page"] == 1
        assert dto["section"] == "s"
        assert dto["content"] == "uniquetokenablephrase"
        assert dto["rank"] == 1
        assert dto["document_version"] == 1
