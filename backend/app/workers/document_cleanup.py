"""cleanup 阶段 worker（T056 / data-model.md 重处理骨架）。

- ``cleanup`` 只清理旧版本派生数据；MVP 版本固定为 1，不存在旧版本，
  本任务作为终态阶段幂等完成（阶段编排器不派生下一阶段、不改变资料状态）；
- 与 ``delete_cleanup`` 职责严格分离（删除清理由 T057 处理）。
"""

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.repositories.fencing import FencingError
from app.services.document_pipeline import DocumentPipelineOrchestrator
from app.workers.base import (
    TaskNotRunnableError,
    begin_attempt,
    converge_cancelled,
    execute_document_task,
)

WORKER_NAME = "orionamesh-cleanup"


def process_cleanup(
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
        orchestrator.complete_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
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

    @celery_app.task(name="orionamesh.document_cleanup", bind=True)
    def cleanup_task(self, task_id: str | uuid.UUID) -> None:
        execute_document_task(task_id, process_cleanup)
