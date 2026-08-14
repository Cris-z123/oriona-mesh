"""任务尝试仓储（T020 / data-model.md attempt 规则）。

- 创建 attempt 必须事务锁定父任务，复制父任务的租户/版本边界并校验调用方提供的
  边界一致；四列冗余边界由数据库五列复合外键作最后一道一致性约束，完整性异常安全
  转换为资源冲突错误；
- attempt ID 是持久化写入的 fencing token；同一任务最多一个 running attempt；
- 读取固定过滤当前用户，未命中统一 ``20007/404``，禁止全局探测。
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import (
    RESOURCE_CONFLICT_MSG,
    RESOURCE_NOT_FOUND_MSG,
)
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import DocumentAttemptStatus

_TENANT_BOUNDARY_FIELDS = ("user_id", "knowledge_base_id", "document_id", "document_version")


class DocumentTaskAttemptRepository:
    """任务尝试仓储；所有操作以当前用户为强制范围。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_for_task(
        self,
        *,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
        worker_name: str,
        started_at: datetime,
    ) -> DocumentTaskAttempt:
        """事务锁定父任务后复制/校验冗余边界并创建 running attempt。

        :raises ApiError: 父任务不在当前用户范围 ``20007/404``；边界不一致或任务
            状态不允许 ``20008/409``。
        """
        task = self.session.scalar(
            select(DocumentTask).where(DocumentTask.id == task_id).with_for_update()
        )
        if task is None:
            raise ApiError(20007, RESOURCE_NOT_FOUND_MSG, 404)

        # 校验调用方提供的边界与父任务一致（不一致视为状态冲突，不创建记录）。
        provided = (user_id, knowledge_base_id, document_id, document_version)
        authoritative = tuple(getattr(task, f) for f in _TENANT_BOUNDARY_FIELDS)
        if provided != authoritative:
            raise ApiError(20008, RESOURCE_CONFLICT_MSG, 409)

        attempt_no = self._next_attempt_no(task.id)
        attempt = DocumentTaskAttempt(
            task_id=task.id,
            user_id=task.user_id,
            knowledge_base_id=task.knowledge_base_id,
            document_id=task.document_id,
            document_version=task.document_version,
            attempt_no=attempt_no,
            worker_name=worker_name,
            status=DocumentAttemptStatus.RUNNING,
            started_at=started_at,
        )
        self.session.add(attempt)
        try:
            self.session.flush()
        except IntegrityError as exc:
            # 数据库复合外键是最后一道一致性约束；触发即视为并发冲突。
            self.session.rollback()
            raise ApiError(20008, RESOURCE_CONFLICT_MSG, 409) from exc
        return attempt

    def get_for_user(self, attempt_id: uuid.UUID, user_id: uuid.UUID) -> DocumentTaskAttempt:
        """按 ID 在当前用户范围内读取 attempt；未命中 ``20007/404``（不全局探测）。"""
        attempt = self.session.scalar(
            select(DocumentTaskAttempt).where(
                DocumentTaskAttempt.id == attempt_id,
                DocumentTaskAttempt.user_id == user_id,
            )
        )
        if attempt is None:
            raise ApiError(20007, RESOURCE_NOT_FOUND_MSG, 404)
        return attempt

    def get_open_for_task(
        self, task_id: uuid.UUID, user_id: uuid.UUID
    ) -> DocumentTaskAttempt | None:
        """返回任务当前未结束（running）的 attempt；读取固定过滤当前用户。"""
        return self.session.scalar(
            select(DocumentTaskAttempt).where(
                DocumentTaskAttempt.task_id == task_id,
                DocumentTaskAttempt.user_id == user_id,
                DocumentTaskAttempt.status == DocumentAttemptStatus.RUNNING,
            )
        )

    def list_for_task(self, task_id: uuid.UUID, user_id: uuid.UUID) -> list[DocumentTaskAttempt]:
        """任务的全部尝试记录（按 attempt_no 升序）；读取固定过滤当前用户。"""
        return list(
            self.session.scalars(
                select(DocumentTaskAttempt)
                .where(
                    DocumentTaskAttempt.task_id == task_id,
                    DocumentTaskAttempt.user_id == user_id,
                )
                .order_by(DocumentTaskAttempt.attempt_no.asc())
            )
        )

    def _next_attempt_no(self, task_id: uuid.UUID) -> int:
        from sqlalchemy import func

        current_max = self.session.scalar(
            select(func.max(DocumentTaskAttempt.attempt_no)).where(
                DocumentTaskAttempt.task_id == task_id
            )
        )
        return int(current_max or 0) + 1
