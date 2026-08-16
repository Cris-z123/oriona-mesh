"""资料任务仓储（T048/T054/T056 / FR-020、data-model.md 任务规则）。

- 读取固定过滤当前用户；未命中统一 ``20007/404``，禁止全局存在性探测；
- 阶段任务按幂等键（``{task_type}:{document_id}:v{version}``、删除清理
  ``delete_cleanup:{document_id}:v{version}:d{delete_cycle}``）创建/激活；
- 每个新任务独立重试计数，与模型网关调用重试相互独立。
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import RESOURCE_NOT_FOUND_MSG
from app.models.document_task import DocumentTask
from app.models.enums import DocumentTaskStatus, DocumentTaskType


def stage_idempotency_key(task_type: DocumentTaskType, document_id: uuid.UUID, version: int) -> str:
    return f"{task_type.value}:{document_id}:v{version}"


def delete_cleanup_idempotency_key(document_id: uuid.UUID, version: int, delete_cycle: int) -> str:
    return f"delete_cleanup:{document_id}:v{version}:d{delete_cycle}"


class DocumentTaskRepository:
    """资料任务仓储（租户范围固定当前用户）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_for_user(self, task_id: uuid.UUID, user_id: uuid.UUID) -> DocumentTask:
        task = self.session.scalar(
            select(DocumentTask).where(DocumentTask.id == task_id, DocumentTask.user_id == user_id)
        )
        if task is None:
            raise ApiError(20007, RESOURCE_NOT_FOUND_MSG, 404)
        return task

    def list_for_document(
        self,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[DocumentTask], int]:
        base = select(DocumentTask).where(
            DocumentTask.document_id == document_id,
            DocumentTask.user_id == user_id,
        )
        total = self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = self.session.scalars(
            base.order_by(DocumentTask.created_at.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(rows), int(total)

    def find_by_idempotency_key(self, key: str) -> DocumentTask | None:
        return self.session.scalar(select(DocumentTask).where(DocumentTask.idempotency_key == key))

    def create_initial_parse(
        self,
        *,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
        status: DocumentTaskStatus,
        queued_at: datetime | None,
    ) -> DocumentTask:
        """创建初始 parse 任务（整批上传；提交后不可执行直到批次协调完成）。"""
        task = DocumentTask(
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            task_type=DocumentTaskType.PARSE,
            delete_cycle=0,
            status=status,
            retry_count=0,
            max_retries=3,
            idempotency_key=stage_idempotency_key(
                DocumentTaskType.PARSE, document_id, document_version
            ),
            queued_at=queued_at,
        )
        self.session.add(task)
        return task

    def requeue(
        self,
        task: DocumentTask,
        *,
        now: datetime,
    ) -> None:
        """重试预算内恢复排队并递增重试计数（worker 失败/失联恢复共用）。"""
        task.status = DocumentTaskStatus.QUEUED
        task.retry_count += 1
        task.queued_at = now
        task.error_code = None
        task.error_message = None
        task.finished_at = None
