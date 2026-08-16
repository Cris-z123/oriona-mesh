"""资料删除编排（T057 / data-model.md 删除资料）。

- DELETE 使用独立且强制 ``user_id`` 的锁定变更查询，可命中普通可见资料、
  ``deleting`` 与 ``failed/delete_cleanup/20015``；``deleted`` 返回 404；
- 首次删除：置 ``deleting``、取消未开始任务、递增 ``delete_cycle``、新建专用
  ``delete_cleanup`` 并同步重置文档镜像计数；命中 ``deleting`` 幂等成功且不递增
  轮次/不建任务；从 ``failed/delete_cleanup/20015`` 重试才递增轮次并以相同规则
  新建任务，旧任务/attempt/retry_count 不可修改；
- 无活动 attempt 立即释放 lease 并激活清理；有活动 attempt 锁定并保留 lease，
  以事务当时的 ``expires_at`` 冻结等待上限（心跳因资料 deleting 不再续租）；
  running attempt 无活动 lease 视为失联并立即接管；
- 清理失败重试耗尽转为 ``failed/delete_cleanup/20015`` 最小墓碑，仅允许
  ``retry_delete``。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import RESOURCE_CONFLICT_MSG, RESOURCE_NOT_FOUND_MSG
from app.core.settings import Settings, get_settings
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
)
from app.repositories.document_task_attempts import DocumentTaskAttemptRepository
from app.repositories.document_tasks import (
    DocumentTaskRepository,
    delete_cleanup_idempotency_key,
)
from app.repositories.documents import DocumentRepository
from app.repositories.processing_leases import ProcessingLeaseRepository
from app.services.file_storage import FileStorage, default_file_storage
from app.workers.base import dispatch_task as _default_dispatch
from app.workers.base import finish_attempt

logger = structlog.get_logger()


def activate_delete_cleanup(
    session: Session,
    *,
    doc: Document,
    now: datetime,
) -> DocumentTask:
    """创建或激活删除清理任务（删除事务与恢复扫描器共用；不投递）。"""
    tasks = DocumentTaskRepository(session)
    key = delete_cleanup_idempotency_key(doc.id, doc.version, doc.delete_cycle)
    task = tasks.find_by_idempotency_key(key)
    if task is None:
        task = DocumentTask(
            user_id=doc.user_id,
            knowledge_base_id=doc.knowledge_base_id,
            document_id=doc.id,
            document_version=doc.version,
            task_type=DocumentTaskType.DELETE_CLEANUP,
            delete_cycle=doc.delete_cycle,
            status=DocumentTaskStatus.PENDING,
            retry_count=0,
            max_retries=3,
            idempotency_key=key,
        )
        session.add(task)
        session.flush()
    elif task.status in (DocumentTaskStatus.RUNNING, DocumentTaskStatus.SUCCEEDED):
        raise ApiError(20008, RESOURCE_CONFLICT_MSG, 409)
    return task


def queue_delete_cleanup(
    session: Session,
    *,
    doc: Document,
    now: datetime,
) -> DocumentTask:
    """把删除清理任务置为 queued 并返回（调用方提交后投递）。"""
    task = activate_delete_cleanup(session, doc=doc, now=now)
    if task.status != DocumentTaskStatus.QUEUED:
        task.status = DocumentTaskStatus.QUEUED
        task.queued_at = now
    return task


class DocumentDeletionService:
    """资料删除编排。"""

    def __init__(
        self,
        session: Session,
        file_storage: FileStorage | None = None,
        dispatch: Callable[[str, tuple], None] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.file_storage = file_storage or default_file_storage()
        self.dispatch = dispatch or _default_dispatch
        self.settings = settings or get_settings()
        self.documents = DocumentRepository(session)
        self.tasks = DocumentTaskRepository(session)
        self.attempts = DocumentTaskAttemptRepository(session)
        self.leases = ProcessingLeaseRepository(session)

    def delete(
        self, user_id: uuid.UUID, knowledge_base_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        doc = self.documents.lock_for_delete(document_id, user_id)
        if doc.knowledge_base_id != knowledge_base_id:
            raise ApiError(20007, RESOURCE_NOT_FOUND_MSG, 404)
        now = datetime.now(UTC)
        if doc.status == DocumentStatus.DELETING:
            return  # 幂等成功：不递增轮次、不创建任务
        cleanup = self.stage_document_delete(doc, user_id, now)
        self.session.commit()
        if cleanup is not None:
            self.dispatch_delete_cleanup(cleanup.id)

    def stage_document_delete(
        self,
        doc: Document,
        user_id: uuid.UUID,
        now: datetime,
    ) -> DocumentTask | None:
        """单资料删除编排（不提交；知识库删除编排与 DELETE 共用）。

        首次删除置 ``deleting``、递增 ``delete_cycle``、取消未开始任务并激活
        ``delete_cleanup``；命中 ``deleting`` 幂等返回 None；``failed/delete_cleanup/
        20015`` 重试递增轮次并新建任务，旧任务/attempt/retry_count 不可修改。

        返回需要投递的 queued 清理任务；返回 None 表示幂等跳过或等待扫描器接管
        （运行 attempt 的等待上限为当时 lease.expires_at，心跳不得续租）。
        """
        if doc.status == DocumentStatus.DELETING:
            return None  # 幂等成功：不递增轮次、不创建任务
        if not doc.is_delete_cleanup_failed:
            # 首次删除：置 deleting 并递增删除轮次。
            doc.status = DocumentStatus.DELETING
        doc.delete_cycle += 1  # 首次删除及 20015 重试均递增轮次
        doc.current_task_type = DocumentTaskType.DELETE_CLEANUP
        doc.retry_count = 0
        doc.error_code = None
        doc.error_message = None

        # 取消未开始任务（pending/queued；不触碰历史终态任务与 attempt）。
        self.session.execute(
            update(DocumentTask)
            .where(
                DocumentTask.document_id == doc.id,
                DocumentTask.user_id == user_id,
                DocumentTask.task_type != DocumentTaskType.DELETE_CLEANUP,
                DocumentTask.status.in_((DocumentTaskStatus.PENDING, DocumentTaskStatus.QUEUED)),
            )
            .values(status=DocumentTaskStatus.CANCELLED, finished_at=now)
        )

        running_attempt = self._open_attempt_for_document(doc.id, user_id)
        lease = self.leases.lock_open(doc.id)
        if running_attempt is None:
            # 无活动 attempt：立即释放 lease 并激活清理。
            if lease is not None:
                self.leases.release(lease.id, reason="deleted", now=now)
            return queue_delete_cleanup(self.session, doc=doc, now=now)
        if lease is None:
            # running attempt 无活动 lease：视为已失联，立即接管。
            finish_attempt(
                self.session,
                running_attempt,
                status=DocumentAttemptStatus.CANCELLED,
                error_message="worker lost without lease",
                now=now,
            )
            attempt_task = self.session.get(DocumentTask, running_attempt.task_id)
            if attempt_task is not None and attempt_task.status == DocumentTaskStatus.RUNNING:
                attempt_task.status = DocumentTaskStatus.CANCELLED
                attempt_task.finished_at = now
            return queue_delete_cleanup(self.session, doc=doc, now=now)
        # 有活动 attempt 且有租约：冻结等待上限（保留 lease；心跳因 deleting 不再续租）。
        activate_delete_cleanup(self.session, doc=doc, now=now)
        return None

    def _open_attempt_for_document(
        self, document_id: uuid.UUID, user_id: uuid.UUID
    ) -> DocumentTaskAttempt | None:
        return self.session.scalar(
            select(DocumentTaskAttempt)
            .join(DocumentTask, DocumentTask.id == DocumentTaskAttempt.task_id)
            .where(
                DocumentTask.document_id == document_id,
                DocumentTaskAttempt.user_id == user_id,
                DocumentTaskAttempt.status == DocumentAttemptStatus.RUNNING,
            )
            .with_for_update()
        )

    def dispatch_delete_cleanup(self, task_id: uuid.UUID) -> None:
        try:
            self.dispatch("orionamesh.document_delete_cleanup", (task_id,))
        except Exception as exc:  # noqa: BLE001 - 投递失败由恢复扫描器重投
            logger.warning(
                "delete_cleanup_dispatch_failed",
                task_id=str(task_id),
                error_type=type(exc).__name__,
            )
