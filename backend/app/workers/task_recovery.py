"""恢复/维护扫描器（T050/T074 / data-model.md 上传恢复、处理并发、删除接管）。

- 上传接管：对超过 ``DOCUMENT_UPLOAD_PENDING_TIMEOUT_SECONDS`` 且仍 ``pending``
  的批次，锁定并复查后调用与上传相同的幂等协调函数；锁不可得时不并发协调；
- 处理名额回收：对 lease 过期且仍 ``running`` 的任务事务性关闭 attempt、释放
  名额，并按重试预算恢复 ``queued`` 或收敛为失败；deleting 资料的超时 lease
  取消 attempt/task 并激活 ``delete_cleanup``；
- 投递兜底：对超过重投阈值仍 ``queued`` 的任务幂等重投（Celery 投递丢失）；
- 幂等清理：删除过期上传幂等记录；
- streaming 消息收敛（T074）：对超过 ``MESSAGE_STREAMING_STALE_SECONDS`` 且仍
  ``streaming`` 的 assistant 消息原子收敛为 ``failed/error``，不得覆盖已终态；
- 全部以 PostgreSQL 记录为真相，Redis/Celery 不作为业务状态来源。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.api.v1.schemas.documents import ASYNC_ERROR_MESSAGES
from app.core.settings import Settings, get_settings
from app.models.conversation import Message
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    MessageRole,
    MessageStatus,
)
from app.models.processing_lease import DocumentProcessingLease
from app.models.upload_request import DocumentUploadRequest
from app.services.document_deletion_service import queue_delete_cleanup
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.services.message_terminal_state import MessageTerminalState
from app.workers.base import dispatch_task as _default_dispatch
from app.workers.base import finish_attempt

logger = structlog.get_logger()

# Celery 投递失败后的重投阈值（代码常量；提交后投递丢失由本扫描器兜底）。
DISPATCH_REDELIVERY_SECONDS = 60

from app.services.document_pipeline import TASK_NAMES  # noqa: E402


def run_maintenance_scan(
    session: Session,
    *,
    storage: FileStorage | None = None,
    dispatch: Callable[[str, tuple], None] | None = None,
    settings: Settings | None = None,
) -> None:
    """周期维护扫描：上传接管 → 名额回收 → 无租约接管 → queued 重投 → 幂等清理 → streaming 收敛。"""
    settings = settings or get_settings()
    dispatch = dispatch or _default_dispatch
    now = datetime.now(UTC)
    scan_upload_batches(session, storage=storage, dispatch=dispatch, now=now)
    scan_expired_leases(session, dispatch=dispatch, now=now)
    scan_running_without_lease(session, dispatch=dispatch, now=now)
    redispatch_stuck_queued_tasks(session, dispatch=dispatch, now=now)
    cleanup_expired_upload_requests(session, now=now)
    converge_stale_streaming_messages(
        session,
        now=now,
        stale_seconds=settings.retrieval.message_streaming_stale_seconds,
    )


def scan_upload_batches(
    session: Session,
    *,
    storage: FileStorage | None,
    dispatch: Callable[[str, tuple], None],
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """接管超过协调窗口仍 pending 的上传批次；返回接管成功的批次。"""
    from app.services.file_storage import default_file_storage

    now = now or datetime.now(UTC)
    settings = get_settings()
    storage = storage or default_file_storage()
    cutoff = now - timedelta(seconds=settings.storage.upload_pending_timeout_seconds)
    batch_ids = (
        session.execute(
            select(Document.upload_batch_id)
            .where(
                Document.status == DocumentStatus.PENDING,
                Document.created_at < cutoff,
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    service = DocumentService(session, file_storage=storage, dispatch=dispatch)
    taken: list[uuid.UUID] = []
    for batch_id in batch_ids:
        request = session.scalar(
            select(DocumentUploadRequest).where(DocumentUploadRequest.upload_batch_id == batch_id)
        )
        try:
            service.coordinate_batch(batch_id, request)
            taken.append(batch_id)
        except Exception:  # noqa: BLE001 - 锁不可得/并发协调：跳过本批次
            session.rollback()
    return taken


def scan_expired_leases(
    session: Session,
    *,
    dispatch: Callable[[str, tuple], None],
    now: datetime | None = None,
) -> int:
    """回收过期处理名额；返回执行接管的数量。"""
    now = now or datetime.now(UTC)
    leases = session.scalars(
        select(DocumentProcessingLease).where(
            DocumentProcessingLease.released_at.is_(None),
            DocumentProcessingLease.expires_at < now,
        )
    ).all()
    recovered = 0
    for lease in leases:
        try:
            if _recover_expired_lease(session, lease, dispatch, now):
                recovered += 1
        except Exception:  # noqa: BLE001 - 单条接管失败不阻塞整轮扫描
            session.rollback()
            logger.warning(
                "lease_recovery_failed",
                lease_id=str(lease.id),
                error_type="recovery",
            )
    return recovered


def scan_running_without_lease(
    session: Session,
    *,
    dispatch: Callable[[str, tuple], None],
    now: datetime | None = None,
) -> int:
    """接管 running 但无开放处理租约的任务（delete_cleanup 失联等）。

    正常阶段任务在 begin_attempt 前必然持有租约；running 且无租约只可能来自
    delete_cleanup（从不获取租约）或失联的异常状态。不处理则资料永久停留在
    ``deleting``/``processing``（data-model.md 删除接管的有界等待要求）。
    """
    now = now or datetime.now(UTC)
    attempt_ids = session.scalars(
        select(DocumentTaskAttempt.id)
        .join(DocumentTask, DocumentTask.id == DocumentTaskAttempt.task_id)
        .join(Document, Document.id == DocumentTask.document_id)
        .where(
            DocumentTaskAttempt.status == DocumentAttemptStatus.RUNNING,
            DocumentTask.status == DocumentTaskStatus.RUNNING,
            ~exists(
                select(DocumentProcessingLease.id).where(
                    DocumentProcessingLease.document_id == Document.id,
                    DocumentProcessingLease.released_at.is_(None),
                )
            ),
        )
    ).all()
    recovered = 0
    for attempt_id in attempt_ids:
        try:
            if _recover_running_without_lease(session, attempt_id, dispatch, now):
                recovered += 1
        except Exception:  # noqa: BLE001 - 单条接管失败不阻塞整轮扫描
            session.rollback()
            logger.warning(
                "running_without_lease_recovery_failed",
                attempt_id=str(attempt_id),
                error_type="recovery",
            )
    return recovered


def _recover_running_without_lease(
    session: Session,
    attempt_id: uuid.UUID,
    dispatch: Callable[[str, tuple], None],
    now: datetime,
) -> bool:
    """单条无租约 running 任务接管：锁定后复查并事务性收敛。"""
    attempt = session.scalar(
        select(DocumentTaskAttempt).where(DocumentTaskAttempt.id == attempt_id).with_for_update()
    )
    if attempt is None or attempt.status != DocumentAttemptStatus.RUNNING:
        session.rollback()
        return False
    task = session.scalar(
        select(DocumentTask).where(DocumentTask.id == attempt.task_id).with_for_update()
    )
    document = session.scalar(
        select(Document).where(Document.id == attempt.document_id).with_for_update()
    )
    if task is None or task.status != DocumentTaskStatus.RUNNING or document is None:
        session.rollback()
        return False
    if (
        document.status == DocumentStatus.DELETING
        and task.task_type == DocumentTaskType.DELETE_CLEANUP
    ):
        # delete_cleanup 失联：按重试预算重排或收敛 20015 墓碑。
        finish_attempt(
            session,
            attempt,
            status=DocumentAttemptStatus.CANCELLED,
            error_message="delete cleanup worker lost",
            now=now,
        )
        if task.retry_count < task.max_retries:
            task.status = DocumentTaskStatus.QUEUED
            task.retry_count += 1
            task.queued_at = now
            task.error_code = None
            task.error_message = None
            document.retry_count = task.retry_count
            session.commit()
            _dispatch_safe(dispatch, DocumentTaskType.DELETE_CLEANUP, task.id)
        else:
            message = ASYNC_ERROR_MESSAGES[20015]
            task.status = DocumentTaskStatus.FAILED
            task.error_code = 20015
            task.error_message = message
            task.finished_at = now
            document.status = DocumentStatus.FAILED
            document.current_task_type = DocumentTaskType.DELETE_CLEANUP
            document.error_code = 20015
            document.error_message = message
            document.processing_finished_at = now
            document.retry_count = task.retry_count
            session.commit()
        return True

    # 删除中资料的普通阶段失联：取消并激活 delete_cleanup。
    finish_attempt(
        session,
        attempt,
        status=DocumentAttemptStatus.CANCELLED,
        error_message="worker lost without lease",
        now=now,
    )
    task.status = DocumentTaskStatus.CANCELLED
    task.finished_at = now
    if document.status == DocumentStatus.DELETING:
        document.current_task_type = DocumentTaskType.DELETE_CLEANUP
        document.retry_count = 0
        cleanup = queue_delete_cleanup(session, doc=document, now=now)
        session.commit()
        _dispatch_safe(dispatch, DocumentTaskType.DELETE_CLEANUP, cleanup.id)
        return True
    # 正常资料失联（异常状态）：按重试预算恢复或失败。
    if task.retry_count < task.max_retries:
        task.status = DocumentTaskStatus.QUEUED
        task.retry_count += 1
        task.queued_at = now
        task.error_code = None
        task.error_message = None
        document.status = DocumentStatus.QUEUED
        document.retry_count = task.retry_count
        document.error_code = None
        document.error_message = None
        session.commit()
        _dispatch_safe(dispatch, task.task_type, task.id)
    else:
        message = ASYNC_ERROR_MESSAGES[20014]
        task.status = DocumentTaskStatus.FAILED
        task.error_code = 20014
        task.error_message = message
        task.finished_at = now
        document.status = DocumentStatus.FAILED
        document.error_code = 20014
        document.error_message = message
        document.processing_finished_at = now
        document.retry_count = task.retry_count
        session.commit()
    return True


def _recover_expired_lease(
    session: Session,
    lease: DocumentProcessingLease | None,
    dispatch: Callable[[str, tuple], None],
    now: datetime,
) -> bool:
    """单条租约接管：锁定 lease/document/task/attempt，事务性收敛。"""
    lease_id = lease.id if lease is not None else None
    lease = session.scalar(
        select(DocumentProcessingLease)
        .where(DocumentProcessingLease.id == lease_id)
        .with_for_update()
    )
    if lease is None or lease.released_at is not None:
        session.rollback()
        return False
    document = session.scalar(
        select(Document).where(Document.id == lease.document_id).with_for_update()
    )
    if document is None:
        lease.released_at = now
        lease.release_reason = "document-missing"
        session.commit()
        return True
    task = (
        session.scalar(
            select(DocumentTask).where(DocumentTask.id == lease.task_id).with_for_update()
        )
        if lease.task_id is not None
        else None
    )
    attempt = (
        session.scalar(
            select(DocumentTaskAttempt)
            .where(
                DocumentTaskAttempt.task_id == task.id,
                DocumentTaskAttempt.user_id == document.user_id,
                DocumentTaskAttempt.status == DocumentAttemptStatus.RUNNING,
            )
            .with_for_update()
        )
        if task is not None
        else None
    )

    if document.status == DocumentStatus.DELETING:
        # 删除接管：取消 attempt/task、释放名额并激活 delete_cleanup。
        if attempt is not None:
            finish_attempt(
                session,
                attempt,
                status=DocumentAttemptStatus.CANCELLED,
                error_message="worker lost while deleting",
                now=now,
            )
        if task is not None and task.status == DocumentTaskStatus.RUNNING:
            task.status = DocumentTaskStatus.CANCELLED
            task.finished_at = now
        lease.released_at = now
        lease.release_reason = "deleted"
        document.current_task_type = DocumentTaskType.DELETE_CLEANUP
        document.retry_count = 0
        cleanup = queue_delete_cleanup(session, doc=document, now=now)
        session.commit()
        _dispatch_safe(dispatch, DocumentTaskType.DELETE_CLEANUP, cleanup.id)
        return True

    # 正常资料失联恢复：关闭 attempt、释放名额，按预算恢复或失败。
    if attempt is not None:
        finish_attempt(
            session,
            attempt,
            status=DocumentAttemptStatus.CANCELLED,
            error_message="worker lease expired",
            now=now,
        )
    lease.released_at = now
    lease.release_reason = "recovery"
    if task is None:
        session.commit()
        return True
    if task.retry_count < task.max_retries:
        task.status = DocumentTaskStatus.QUEUED
        task.retry_count += 1
        task.queued_at = now
        task.error_code = None
        task.error_message = None
        document.status = DocumentStatus.QUEUED
        document.retry_count = task.retry_count
        document.error_code = None
        document.error_message = None
        session.commit()
        _dispatch_safe(dispatch, task.task_type, task.id)
    else:
        message = ASYNC_ERROR_MESSAGES[20014]
        task.status = DocumentTaskStatus.FAILED
        task.error_code = 20014
        task.error_message = message
        task.finished_at = now
        document.status = DocumentStatus.FAILED
        document.error_code = 20014
        document.error_message = message
        document.processing_finished_at = now
        document.retry_count = task.retry_count
        session.commit()
    return True


def redispatch_stuck_queued_tasks(
    session: Session,
    *,
    dispatch: Callable[[str, tuple], None],
    now: datetime | None = None,
) -> int:
    """对超过重投阈值仍 queued 的任务幂等重投（投递丢失兜底）。"""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=DISPATCH_REDELIVERY_SECONDS)
    tasks = session.scalars(
        select(DocumentTask).where(
            DocumentTask.status == DocumentTaskStatus.QUEUED,
            DocumentTask.queued_at < cutoff,
        )
    ).all()
    for task in tasks:
        _dispatch_safe(dispatch, task.task_type, task.id)
    return len(tasks)


def cleanup_expired_upload_requests(
    session: Session,
    *,
    now: datetime | None = None,
) -> int:
    """删除过期上传幂等记录（默认保留 24 小时）。"""
    from app.repositories.upload_requests import UploadRequestRepository

    now = now or datetime.now(UTC)
    removed = UploadRequestRepository(session).delete_expired(now)
    if removed:
        session.commit()
    return removed


def converge_stale_streaming_messages(
    session: Session,
    *,
    now: datetime | None = None,
    stale_seconds: int,
) -> int:
    """收敛超过失联上限仍为 ``streaming`` 的 assistant 消息（T074 / FR-018）。

    API 进程崩溃或终态写入中断后，把 ``status=streaming AND created_at <
    now() - MESSAGE_STREAMING_STALE_SECONDS`` 的消息原子收敛为 ``failed/error``；
    终态收敛器只改写仍为 streaming 的消息，已终态消息（含用户消息）不受影响。
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=stale_seconds)
    messages = session.scalars(
        select(Message).where(
            Message.role == MessageRole.ASSISTANT,
            Message.status == MessageStatus.STREAMING,
            Message.created_at < cutoff,
        )
    ).all()
    writer = MessageTerminalState(session)
    converged = 0
    for message in messages:
        if writer.fail(message.id, message.user_id):
            converged += 1
    return converged


def _dispatch_safe(
    dispatch: Callable[[str, tuple], None], task_type: DocumentTaskType, task_id: uuid.UUID
) -> None:
    try:
        dispatch(TASK_NAMES[task_type], (task_id,))
    except Exception:  # noqa: BLE001 - 投递失败由下一轮扫描重投
        logger.warning(
            "scan_dispatch_failed",
            task_type=task_type.value,
            task_id=str(task_id),
            error_type="dispatch",
        )
