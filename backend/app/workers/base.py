"""worker 公共设施（T050 / data-model.md 任务规则）。

- 提交后投递适配层：``dispatch_task`` 只负责投递，失败由恢复扫描器重投；
- ``begin_attempt``：任务必须处于 ``queued`` 才可执行（pending 初始任务不可执行、
  重复投递幂等跳过），创建 running attempt 并记录非空 ``started_at``；
- 初次执行 attempt_no=1/retry_count=0；每次重试先递增计数再创建下一个 attempt；
  ``max_retries=3`` 时单任务最多 4 个 attempt。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import DocumentTaskStatus
from app.repositories.document_task_attempts import DocumentTaskAttemptRepository

logger = structlog.get_logger()


class TaskNotRunnableError(Exception):
    """任务不可执行（pending 未就绪、重复投递或已达重试预算）；worker 静默跳过。"""


def dispatch_task(name: str, args: tuple[object, ...]) -> None:
    """提交后投递 Celery 任务；投递失败只记日志，DB 真相由扫描器重投。

    Celery 任务只接收字符串 ``task_id``（租户边界在任务内部从任务行加载）。领域层仍使用
    UUID，但传输边界必须把它转换为标量，避免依赖 Celery serializer 对 UUID 的实现细节。
    """
    from app.workers.celery_app import celery_app

    try:
        celery_app.send_task(
            name,
            args=tuple(str(value) if isinstance(value, uuid.UUID) else value for value in args),
        )
    except Exception as exc:  # noqa: BLE001 - 投递失败由恢复扫描器重投
        logger.warning("task_dispatch_failed", task_name=name, error_type=type(exc).__name__)


def normalize_task_id(task_id: str | uuid.UUID) -> uuid.UUID:
    """将 Celery 载荷和历史直接调用统一为领域 UUID。"""
    return task_id if isinstance(task_id, uuid.UUID) else uuid.UUID(task_id)


def execute_document_task(task_id: str | uuid.UUID, process: Callable[..., None]) -> None:
    """加载资料任务边界并调用一个阶段处理器。

    所有资料 worker 共用此入口，确保跨进程的 UUID 只在此规范化一次。执行异常仍交由
    Celery 记录；阶段处理器内部负责 attempt、重试和业务状态的收敛。
    """
    from app.infrastructure.database.session import SessionLocal

    normalized_task_id = normalize_task_id(task_id)
    session = SessionLocal()
    try:
        bounds = load_task_boundaries(session, normalized_task_id)
        if bounds is None:
            return
        user_id, knowledge_base_id, document_id, document_version = bounds
        process(
            session,
            task_id=normalized_task_id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
        )
    finally:
        session.close()


def load_task_boundaries(
    session: Session, task_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, int] | None:
    """Celery 包装器从任务行加载租户/版本边界（任务本身是权威来源）。"""
    task = session.scalar(select(DocumentTask).where(DocumentTask.id == task_id))
    if task is None:
        return None
    return task.user_id, task.knowledge_base_id, task.document_id, task.document_version


def begin_attempt(
    session: Session,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    document_version: int,
    worker_name: str,
) -> tuple[DocumentTask, DocumentTaskAttempt]:
    """事务锁定任务并创建 running attempt。

    :raises TaskNotRunnableError: 任务非 queued（pending 初始任务、重复投递或已终态）。
    """
    task = session.scalar(select(DocumentTask).where(DocumentTask.id == task_id).with_for_update())
    if task is None:
        raise TaskNotRunnableError("task not found")
    if task.status != DocumentTaskStatus.QUEUED:
        raise TaskNotRunnableError(f"task not runnable: {task.status.value}")
    # retry_count == max_retries 时仍允许最后第 max_retries+1 次尝试
    # （预算由 fail_stage/恢复扫描器在重排时检查）。
    if task.retry_count > task.max_retries:
        raise TaskNotRunnableError("task retry budget exhausted")
    now = datetime.now(UTC)
    attempt = DocumentTaskAttemptRepository(session).create_for_task(
        task_id=task.id,
        user_id=user_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        document_version=document_version,
        worker_name=worker_name,
        started_at=now,
    )
    task.status = DocumentTaskStatus.RUNNING
    if task.started_at is None:
        task.started_at = now
    session.commit()
    return task, attempt


def finish_attempt(
    session: Session,
    attempt: DocumentTaskAttempt,
    *,
    status,
    error_message: str | None = None,
    now: datetime | None = None,
) -> None:
    """关闭 attempt（成功/失败/取消共用），记录耗时与安全错误摘要。"""
    now = now or datetime.now(UTC)
    attempt.status = status
    attempt.finished_at = now
    attempt.error_message = error_message
    started = attempt.started_at
    attempt.duration_ms = max(int((now - started).total_seconds() * 1000), 0)


def converge_cancelled(session: Session, *, attempt_id: uuid.UUID) -> None:
    """fencing 拒绝写入后的取消收敛：attempt/task 置 cancelled（删除接管由扫描器完成）。"""
    from app.models.enums import DocumentAttemptStatus, DocumentTaskStatus

    attempt = session.scalar(
        select(DocumentTaskAttempt).where(DocumentTaskAttempt.id == attempt_id).with_for_update()
    )
    if attempt is None or attempt.status != DocumentAttemptStatus.RUNNING:
        session.rollback()
        return
    now = datetime.now(UTC)
    finish_attempt(
        session,
        attempt,
        status=DocumentAttemptStatus.CANCELLED,
        error_message="fencing rejected write",
        now=now,
    )
    task = session.scalar(
        select(DocumentTask).where(DocumentTask.id == attempt.task_id).with_for_update()
    )
    if task is not None and task.status == DocumentTaskStatus.RUNNING:
        task.status = DocumentTaskStatus.CANCELLED
        task.finished_at = now
    session.commit()
