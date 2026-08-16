"""流水线统一编排器（T056 / data-model.md 阶段切换与 finalize）。

- worker 不得自行拼接下一阶段；``complete_stage`` 在一个事务内锁定并校验当前
  attempt/task/document/lease，把 attempt/task 标为 ``succeeded``，按阶段幂等键
  创建或激活下一任务，更新 ``documents.current_task_type`` 与 ``lease.task_id``
  后提交；只在提交后投递 Celery，投递失败由恢复扫描器重投（DB 真相不丢）；
- 幂等重放：当前 attempt 已终态时校验下一任务 queued 并重投，不创建重复任务；
- ``finalize`` 只经 :class:`ChunkRepository` 校验正式片段数量与版本后原子翻转
  ``completed``/``chunk_count`` 并释放处理名额；数量不一致持久化 ``20013``；
- ``fail_stage``：确定性业务失败（error_code 非空）立即收敛资料失败并释放名额；
  未归类异常按重试预算恢复排队（``documents.retry_count`` 镜像当前任务），
  预算耗尽持久化 ``20014``；
- ``cleanup`` 只清理旧版本（MVP 无旧版本，作为终态阶段幂等完成）。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.schemas.documents import ASYNC_ERROR_MESSAGES
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
)
from app.repositories.chunks import ChunkRepository
from app.repositories.document_tasks import (
    DocumentTaskRepository,
    stage_idempotency_key,
)
from app.repositories.fencing import FencingError
from app.repositories.processing_leases import ProcessingLeaseRepository
from app.workers.base import dispatch_task as _default_dispatch
from app.workers.base import finish_attempt

logger = structlog.get_logger()

# 阶段切换映射；finalize/cleanup/delete_cleanup 为终态阶段。
_NEXT_TASK_TYPE: dict[DocumentTaskType, DocumentTaskType] = {
    DocumentTaskType.PARSE: DocumentTaskType.CHUNK,
    DocumentTaskType.CHUNK: DocumentTaskType.EMBED,
    DocumentTaskType.EMBED: DocumentTaskType.FINALIZE,
}

TASK_NAMES: dict[DocumentTaskType, str] = {
    DocumentTaskType.PARSE: "orionamesh.document_parse",
    DocumentTaskType.CHUNK: "orionamesh.document_chunk",
    DocumentTaskType.EMBED: "orionamesh.document_embed",
    DocumentTaskType.FINALIZE: "orionamesh.document_finalize",
    DocumentTaskType.CLEANUP: "orionamesh.document_cleanup",
    DocumentTaskType.DELETE_CLEANUP: "orionamesh.document_delete_cleanup",
}


@dataclass(frozen=True)
class StageResult:
    """阶段完成结果（编排器持久化到任务记录）。"""

    total_items: int | None = None
    processed_items: int | None = None
    chunk_count: int | None = None  # finalize 期望的正式片段数量


class DocumentPipelineOrchestrator:
    """统一阶段编排器。"""

    def __init__(
        self, session: Session, dispatch: Callable[[str, tuple], None] | None = None
    ) -> None:
        self.session = session
        self.dispatch = dispatch or _default_dispatch
        self.tasks = DocumentTaskRepository(session)
        self.leases = ProcessingLeaseRepository(session)

    # ------------------------------------------------------------------
    # 阶段成功
    # ------------------------------------------------------------------
    def complete_stage(
        self,
        *,
        attempt_id: uuid.UUID,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
        result: StageResult | None = None,
    ) -> None:
        attempt, task, document = self._lock_all(attempt_id, document_id)
        if attempt.status != DocumentAttemptStatus.RUNNING:
            # 幂等重放：当前阶段已终态；下一阶段已激活则重投，不创建重复任务。
            self._replay_after_terminal(task, document)
            return
        self._validate_running(attempt, task, document, document_version)

        if task.task_type in (DocumentTaskType.FINALIZE, DocumentTaskType.CLEANUP):
            if task.task_type == DocumentTaskType.FINALIZE:
                self._complete_finalize(attempt, task, document, result)
            else:
                # cleanup 只清理旧版本：MVP 无旧版本，作为终态阶段幂等完成。
                self._complete_terminal(attempt, task)
            return

        next_type = _NEXT_TASK_TYPE[task.task_type]
        now = datetime.now(UTC)
        finish_attempt(self.session, attempt, status=DocumentAttemptStatus.SUCCEEDED, now=now)
        task.status = DocumentTaskStatus.SUCCEEDED
        task.finished_at = now
        if result is not None:
            if result.total_items is not None:
                task.total_items = result.total_items
            if result.processed_items is not None:
                task.processed_items = result.processed_items

        # 幂等创建或激活下一任务。
        next_task = self.tasks.find_by_idempotency_key(
            stage_idempotency_key(next_type, document_id, document_version)
        )
        if next_task is None:
            next_task = DocumentTask(
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version=document_version,
                task_type=next_type,
                delete_cycle=0,
                status=DocumentTaskStatus.QUEUED,
                retry_count=0,
                max_retries=3,
                idempotency_key=stage_idempotency_key(next_type, document_id, document_version),
                queued_at=now,
            )
            self.session.add(next_task)
        elif next_task.status not in (DocumentTaskStatus.PENDING, DocumentTaskStatus.QUEUED):
            raise FencingError("next task already terminal")
        # 新任务 ID 由数据库生成：先 flush 取得 ID，再写入 lease.task_id（同事务）。
        self.session.flush()

        # 同事务：current_task_type、文档重试镜像归零、lease.task_id。
        document.current_task_type = next_type
        document.retry_count = 0
        lease = self.leases.lock_open(document_id)
        if lease is None:
            raise FencingError("processing lease missing")
        lease.task_id = next_task.id
        self.session.commit()
        # 提交后才投递；投递失败由扫描器重投。
        self._dispatch_safe(next_type, next_task.id)

    # ------------------------------------------------------------------
    # 阶段失败
    # ------------------------------------------------------------------
    def fail_stage(
        self,
        *,
        attempt_id: uuid.UUID,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
        error_code: int | None = None,
        error_message: str | None = None,
    ) -> None:
        attempt, task, document = self._lock_all(attempt_id, document_id)
        if attempt.status != DocumentAttemptStatus.RUNNING:
            return  # 幂等
        now = datetime.now(UTC)
        if error_code is not None:
            # 确定性业务失败：立即收敛资料失败并释放名额。
            message = error_message or ASYNC_ERROR_MESSAGES.get(error_code, error_message or "")
            finish_attempt(
                self.session,
                attempt,
                status=DocumentAttemptStatus.FAILED,
                error_message=message,
                now=now,
            )
            task.status = DocumentTaskStatus.FAILED
            task.error_code = error_code
            task.error_message = message
            task.finished_at = now
            document.status = DocumentStatus.FAILED
            document.error_code = error_code
            document.error_message = message
            document.processing_finished_at = now
            document.retry_count = task.retry_count
            self._release_lease(document_id, "failed", now)
            self.session.commit()
            return
        # 未归类异常：按重试预算恢复（任务级重试与模型网关重试相互独立）。
        if task.retry_count < task.max_retries:
            finish_attempt(
                self.session,
                attempt,
                status=DocumentAttemptStatus.FAILED,
                error_message="worker execution failed",
                now=now,
            )
            self.tasks.requeue(task, now=now)
            document.retry_count = task.retry_count
            document.error_code = None
            document.error_message = None
            self.session.commit()
            self._dispatch_safe(task.task_type, task.id)
            return
        message = ASYNC_ERROR_MESSAGES[20014]
        finish_attempt(
            self.session,
            attempt,
            status=DocumentAttemptStatus.FAILED,
            error_message=message,
            now=now,
        )
        task.status = DocumentTaskStatus.FAILED
        task.error_code = 20014
        task.error_message = message
        task.finished_at = now
        document.status = DocumentStatus.FAILED
        document.error_code = 20014
        document.error_message = message
        document.processing_finished_at = now
        document.retry_count = task.retry_count
        self._release_lease(document_id, "failed", now)
        self.session.commit()

    # ------------------------------------------------------------------
    # finalize
    # ------------------------------------------------------------------
    def _complete_finalize(
        self,
        attempt: DocumentTaskAttempt,
        task: DocumentTask,
        document: Document,
        result: StageResult | None,
    ) -> None:
        now = datetime.now(UTC)
        expected = (
            result.chunk_count
            if result is not None and result.chunk_count is not None
            else task.processed_items
        ) or 0
        count = ChunkRepository(self.session).count_for_pipeline(
            document.user_id, document.knowledge_base_id, document.id, document.version
        )
        if count != expected:
            # 发布校验失败：数量不一致持久化 20013，未发布片段仍不可检索。
            message = ASYNC_ERROR_MESSAGES[20013]
            finish_attempt(
                self.session,
                attempt,
                status=DocumentAttemptStatus.FAILED,
                error_message=message,
                now=now,
            )
            task.status = DocumentTaskStatus.FAILED
            task.error_code = 20013
            task.error_message = message
            task.finished_at = now
            document.status = DocumentStatus.FAILED
            document.error_code = 20013
            document.error_message = message
            document.processing_finished_at = now
            document.retry_count = task.retry_count
            self._release_lease(document.id, "finalize_failed", now)
            self.session.commit()
            return
        finish_attempt(self.session, attempt, status=DocumentAttemptStatus.SUCCEEDED, now=now)
        task.status = DocumentTaskStatus.SUCCEEDED
        task.finished_at = now
        task.total_items = count
        task.processed_items = count
        document.status = DocumentStatus.COMPLETED
        document.chunk_count = count
        document.current_task_type = None
        document.retry_count = 0
        document.processing_finished_at = now
        self._release_lease(document.id, "completed", now)
        self.session.commit()

    def _complete_terminal(self, attempt: DocumentTaskAttempt, task: DocumentTask) -> None:
        """cleanup 等终态阶段：仅标记成功，不改变资料状态。"""
        now = datetime.now(UTC)
        finish_attempt(self.session, attempt, status=DocumentAttemptStatus.SUCCEEDED, now=now)
        task.status = DocumentTaskStatus.SUCCEEDED
        task.finished_at = now
        self.session.commit()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _lock_all(
        self, attempt_id: uuid.UUID, document_id: uuid.UUID
    ) -> tuple[DocumentTaskAttempt, DocumentTask, Document]:
        attempt = self.session.scalar(
            select(DocumentTaskAttempt)
            .where(DocumentTaskAttempt.id == attempt_id)
            .with_for_update()
        )
        if attempt is None:
            raise FencingError("attempt not found")
        task = self.session.scalar(
            select(DocumentTask).where(DocumentTask.id == attempt.task_id).with_for_update()
        )
        if task is None:
            raise FencingError("task not found")
        document = self.session.scalar(
            select(Document).where(Document.id == document_id).with_for_update()
        )
        if document is None:
            raise FencingError("document not found")
        return attempt, task, document

    def _validate_running(
        self,
        attempt: DocumentTaskAttempt,
        task: DocumentTask,
        document: Document,
        document_version: int,
    ) -> None:
        if task.status != DocumentTaskStatus.RUNNING:
            raise FencingError("task is not running")
        if document.status in (DocumentStatus.DELETING, DocumentStatus.DELETED):
            raise FencingError("document is being deleted")
        if document.version != document_version:
            raise FencingError("document version mismatch")

    def _replay_after_terminal(self, task: DocumentTask, document: Document) -> None:
        if task.status != DocumentTaskStatus.SUCCEEDED:
            return
        next_type = _NEXT_TASK_TYPE.get(task.task_type)
        if next_type is None:
            return
        next_task = self.tasks.find_by_idempotency_key(
            stage_idempotency_key(next_type, document.id, document.version)
        )
        if next_task is not None and next_task.status == DocumentTaskStatus.QUEUED:
            self.session.commit()
            self._dispatch_safe(next_type, next_task.id)

    def _release_lease(self, document_id: uuid.UUID, reason: str, now: datetime) -> None:
        lease = self.leases.lock_open(document_id)
        if lease is not None:
            self.leases.release(lease.id, reason=reason, now=now)

    def _dispatch_safe(self, task_type: DocumentTaskType, task_id: uuid.UUID) -> None:
        try:
            self.dispatch(TASK_NAMES[task_type], (task_id,))
        except Exception:  # noqa: BLE001 - 投递失败由恢复扫描器重投，DB 真相不丢
            logger.warning(
                "task_dispatch_failed",
                task_type=task_type.value,
                task_id=str(task_id),
                error_type="dispatch",
            )
