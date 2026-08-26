"""delete_cleanup 阶段 worker（T057 / data-model.md 删除资料）。

- 清理原始对象、解析结果对象与行、草稿与正式片段；历史引用外键置空但保留
  必填快照（可核验、不可恢复原始资料）；
- 清理成功后保留不可查询的 ``deleted`` 墓碑；
- 清理失败按重试预算恢复；预算耗尽转为 ``failed/delete_cleanup/20015`` 最小
  “删除未完成”墓碑，仅向所属用户提供重试删除。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api.v1.schemas.documents import ASYNC_ERROR_MESSAGES
from app.core.settings import Settings, get_settings
from app.models.chunk import DocumentChunkDraft, DocumentParseResult
from app.models.conversation import MessageCitation
from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
)
from app.repositories.chunks import ChunkRepository
from app.repositories.document_tasks import DocumentTaskRepository
from app.repositories.processing_leases import ProcessingLeaseRepository
from app.services.file_storage import FileStorage, default_file_storage
from app.workers.base import (
    TaskNotRunnableError,
    begin_attempt,
    execute_document_task,
    finish_attempt,
)

WORKER_NAME = "orionamesh-delete-cleanup"


def process_delete_cleanup(
    session: Session,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    document_version: int,
    file_storage: FileStorage | None = None,
    dispatch: Callable[[str, tuple], None] | None = None,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    file_storage = file_storage or default_file_storage()
    if dispatch is None:
        from app.workers.base import dispatch_task

        dispatch = dispatch_task
    leases = ProcessingLeaseRepository(session)

    try:
        task, attempt = begin_attempt(
            session,
            task_id=task_id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            worker_name=WORKER_NAME,
        )
    except TaskNotRunnableError:
        return

    now = datetime.now(UTC)
    doc = session.get(Document, document_id)
    if doc is None or doc.status == DocumentStatus.DELETED:
        # 已删除/不存在：幂等成功。
        finish_attempt(session, attempt, status=DocumentAttemptStatus.SUCCEEDED, now=now)
        task.status = DocumentTaskStatus.SUCCEEDED
        task.finished_at = now
        session.commit()
        return

    try:
        # 原始对象与解析结果对象删除（外部 I/O 先做，不持事务）。
        file_storage.delete_object(doc.storage_path)
        results = session.scalars(
            select(DocumentParseResult).where(
                DocumentParseResult.document_id == document_id,
                DocumentParseResult.user_id == user_id,
            )
        ).all()
        for result in results:
            file_storage.delete_object(result.content_object_key)
            session.delete(result)
        # 草稿与正式片段（统一仓储删除）。
        session.execute(
            delete(DocumentChunkDraft).where(
                DocumentChunkDraft.document_id == document_id,
                DocumentChunkDraft.user_id == user_id,
            )
        )
        ChunkRepository(session).delete_for_document(user_id, knowledge_base_id, document_id)
        # 历史引用：外键置空、保留必填快照（不可恢复原始资料）。
        session.execute(
            update(MessageCitation)
            .where(
                MessageCitation.document_id == document_id,
                MessageCitation.user_id == user_id,
            )
            .values(chunk_id=None, document_id=None)
        )
        # 墓碑。
        doc.status = DocumentStatus.DELETED
        doc.current_task_type = None
        doc.error_code = None
        doc.error_message = None
        doc.processing_finished_at = now
        lease = leases.find_open(document_id)
        if lease is not None:
            leases.release(lease.id, reason="deleted", now=now)
        finish_attempt(session, attempt, status=DocumentAttemptStatus.SUCCEEDED, now=now)
        task.status = DocumentTaskStatus.SUCCEEDED
        task.finished_at = now
        session.commit()
    except Exception:
        session.rollback()
        _fail_cleanup(session, task=task, attempt=attempt, doc=doc, dispatch=dispatch, now=now)


def _fail_cleanup(
    session: Session,
    *,
    task: DocumentTask,
    attempt,
    doc: Document,
    dispatch: Callable[[str, tuple], None],
    now: datetime,
) -> None:
    """清理失败：按重试预算恢复；预算耗尽转为 20015 删除未完成墓碑。"""
    message = ASYNC_ERROR_MESSAGES[20015]
    finish_attempt(
        session,
        attempt,
        status=DocumentAttemptStatus.FAILED,
        error_message=message,
        now=now,
    )
    if task.retry_count < task.max_retries:
        DocumentTaskRepository(session).requeue(task, now=now)
        doc.retry_count = task.retry_count
        session.commit()
        try:
            dispatch("orionamesh.document_delete_cleanup", (task.id,))
        except Exception:  # noqa: BLE001 - 投递失败由恢复扫描器重投
            pass
        return
    task.status = DocumentTaskStatus.FAILED
    task.error_code = 20015
    task.error_message = message
    task.finished_at = now
    doc.status = DocumentStatus.FAILED
    doc.current_task_type = DocumentTaskType.DELETE_CLEANUP
    doc.error_code = 20015
    doc.error_message = message
    doc.processing_finished_at = now
    doc.retry_count = task.retry_count
    session.commit()


def register_tasks(celery_app) -> None:
    """注册 Celery 任务（提交后投递适配层）。"""

    @celery_app.task(name="orionamesh.document_delete_cleanup", bind=True)
    def delete_cleanup_task(self, task_id: str | uuid.UUID) -> None:
        execute_document_task(task_id, process_delete_cleanup)
