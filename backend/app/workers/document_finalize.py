"""finalize 阶段 worker（T056 / data-model.md finalize 边界）。

- 只经 :class:`ChunkRepository` 校验正式片段数量/版本与任务结果一致后原子翻转
  ``completed``/``chunk_count`` 并释放处理名额（统一编排器执行）；
- 数量不一致持久化 ``20013``，未发布片段仍不可检索；
- 不搬运或复制片段。
"""

import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.models.document_task import DocumentTask
from app.models.enums import DocumentTaskType
from app.repositories.fencing import FencingError
from app.services.document_pipeline import DocumentPipelineOrchestrator, StageResult
from app.workers.base import (
    TaskNotRunnableError,
    begin_attempt,
    converge_cancelled,
    load_task_boundaries,
)

WORKER_NAME = "orionamesh-finalize"


def process_finalize(
    session: Session,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    document_version: int,
    dispatch: Callable[[str, tuple], None] | None = None,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    orchestrator = DocumentPipelineOrchestrator(session, dispatch=dispatch)

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

    try:
        # 期望片段数来自 embed 阶段检查点（processed_items）。
        embed_task = session.scalar(
            select(DocumentTask)
            .where(
                DocumentTask.document_id == document_id,
                DocumentTask.user_id == user_id,
                DocumentTask.task_type == DocumentTaskType.EMBED,
            )
            .order_by(DocumentTask.created_at.desc())
            .limit(1)
        )
        expected = embed_task.processed_items if embed_task is not None else 0
        orchestrator.complete_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            result=StageResult(chunk_count=expected),
        )
    except FencingError:
        session.rollback()
        converge_cancelled(session, attempt_id=attempt.id)
    except Exception:
        session.rollback()
        orchestrator.fail_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            error_code=None,
        )


def register_tasks(celery_app) -> None:
    """注册 Celery 任务（提交后投递适配层）。"""

    @celery_app.task(name="orionamesh.document_finalize", bind=True)
    def finalize_task(self, task_id: str) -> None:
        from app.infrastructure.database.session import SessionLocal

        session = SessionLocal()
        try:
            bounds = load_task_boundaries(session, uuid.UUID(task_id))
            if bounds is None:
                return
            user_id, knowledge_base_id, document_id, document_version = bounds
            process_finalize(
                session,
                task_id=uuid.UUID(task_id),
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version=document_version,
            )
        finally:
            session.close()
