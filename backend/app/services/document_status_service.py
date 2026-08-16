"""资料与任务详情服务（T080 / FR-005、FR-007、FR-010、FR-034）。

- 统一资料/任务详情查询：终态、当前阶段、完整尝试记录（worker、非空
  ``started_at``、可空结束/错误/耗时）；
- ``20001/20010~20015/50000`` 持久化失败码与固定安全提示的错误分类；
  失败原因与诊断信息只对所属用户返回（租户范围固定当前用户）；
- ``failed/delete_cleanup/20015`` 映射为“删除未完成”最小墓碑与
  ``retry_delete``，而不是普通处理失败；
- 内容与子资源读取只允许 ``active`` 知识库（data-model.md 知识库边界）；
  ``deleting``/``delete_failed`` 知识库的子资源统一 ``20002/404``。
"""

import uuid

from sqlalchemy.orm import Session

from app.api.v1.schemas.documents import document_dto, document_task_dto
from app.models.enums import DocumentStatus
from app.repositories.base import require_active_knowledge_base
from app.repositories.document_task_attempts import DocumentTaskAttemptRepository
from app.repositories.document_tasks import DocumentTaskRepository
from app.repositories.documents import DocumentRepository


class DocumentStatusService:
    """资料与任务详情查询服务。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.documents = DocumentRepository(session)
        self.tasks = DocumentTaskRepository(session)
        self.attempts = DocumentTaskAttemptRepository(session)

    # ------------------------------------------------------------------
    # 资料详情与列表
    # ------------------------------------------------------------------
    def get_document(
        self,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> dict:
        """当前用户范围内可见资料详情；隐藏资料统一 ``20007/404``。"""
        require_active_knowledge_base(self.session, knowledge_base_id, user_id)
        doc = self.documents.get_visible(document_id, knowledge_base_id, user_id)
        return document_dto(doc)

    def list_documents(
        self,
        knowledge_base_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
        status: DocumentStatus | None = None,
    ) -> dict:
        """分页列出可见资料；先排除内部 deleting/deleted，再应用公开状态过滤。"""
        require_active_knowledge_base(self.session, knowledge_base_id, user_id)
        items, total = self.documents.list_visible(
            knowledge_base_id,
            user_id,
            page=page,
            page_size=page_size,
            status=status,
        )
        return {
            "items": [document_dto(doc) for doc in items],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    # ------------------------------------------------------------------
    # 任务与尝试记录
    # ------------------------------------------------------------------
    def list_tasks(
        self,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
    ) -> dict:
        """资料任务列表：阶段枚举、delete_cycle、持久化失败码与完整尝试记录。"""
        require_active_knowledge_base(self.session, knowledge_base_id, user_id)
        # 资料必须当前用户范围内可见（deleting/deleted 统一 20007/404）。
        self.documents.get_visible(document_id, knowledge_base_id, user_id)
        tasks, total = self.tasks.list_for_document(
            document_id, user_id, page=page, page_size=page_size
        )
        items = [
            document_task_dto(task, self.attempts.list_for_task(task.id, user_id)) for task in tasks
        ]
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
        }
