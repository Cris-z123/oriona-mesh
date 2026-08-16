"""资料仓储（T047/T048/T057 / FR-020）。

- 读取固定过滤当前用户；未命中统一 ``20007/404``，禁止全局存在性探测；
- 列表查询先固定排除内部 ``deleting/deleted`` 再应用公开 ``status`` 过滤；
- 批次协调使用 ``SELECT ... FOR UPDATE SKIP LOCKED``（data-model.md 上传恢复）；
- ``lock_for_delete`` 仅供资料 DELETE 使用，可命中普通可见资料、``deleting`` 与
  ``failed/delete_cleanup/20015``，禁止复用于 GET/list。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import RESOURCE_NOT_FOUND_MSG
from app.models.document import Document
from app.models.enums import DocumentStatus, DocumentTaskType

# 内部隐藏状态：普通读取一律排除。
_HIDDEN_STATUSES = (DocumentStatus.DELETING, DocumentStatus.DELETED)


class DocumentRepository:
    """资料仓储（租户范围固定当前用户）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, document_id: uuid.UUID, knowledge_base_id: uuid.UUID, user_id: uuid.UUID
    ) -> Document:
        """当前用户范围内读取可见资料；未命中 ``20007/404``（不全局探测）。"""
        doc = self.session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.knowledge_base_id == knowledge_base_id,
                Document.user_id == user_id,
                Document.status.not_in(_HIDDEN_STATUSES),
            )
        )
        if doc is None:
            raise ApiError(20007, RESOURCE_NOT_FOUND_MSG, 404)
        return doc

    def list_visible(
        self,
        knowledge_base_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
        status: DocumentStatus | None = None,
    ) -> tuple[list[Document], int]:
        """分页列出可见资料；先排除 deleting/deleted，再应用公开状态过滤。"""
        from sqlalchemy import func

        base = select(Document).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.user_id == user_id,
            Document.status.not_in(_HIDDEN_STATUSES),
        )
        if status is not None:
            base = base.where(Document.status == status)
        total = self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = self.session.scalars(
            base.order_by(Document.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(rows), int(total)

    def lock_batch_for_coordination(
        self, upload_batch_id: uuid.UUID
    ) -> list[Document]:
        """锁定整批资料（FOR UPDATE SKIP LOCKED）；锁不可得时返回空列表。

        协调器在持有该短事务行锁期间完成同卷原子重命名；扫描器拿不到锁时
        不并发协调（data-model.md 上传恢复）。
        """
        rows = self.session.scalars(
            select(Document)
            .where(Document.upload_batch_id == upload_batch_id)
            .order_by(Document.id)
            .with_for_update(skip_locked=True)
        ).all()
        return list(rows)

    def lock_for_delete(self, document_id: uuid.UUID, user_id: uuid.UUID) -> Document:
        """仅 DELETE 使用的锁定变更查询；命中可见资料/deleting/删除失败态。

        ``deleted`` 或不存在统一 ``20007/404``；不得复用于普通读取。
        """
        doc = self.session.scalar(
            select(Document)
            .where(Document.id == document_id, Document.user_id == user_id)
            .with_for_update()
        )
        if doc is None or doc.status == DocumentStatus.DELETED:
            raise ApiError(20007, RESOURCE_NOT_FOUND_MSG, 404)
        return doc

    def is_delete_cleanup_failed(self, doc: Document) -> bool:
        """资料是否为 failed/delete_cleanup/20015 删除未完成墓碑。"""
        return (
            doc.status == DocumentStatus.FAILED
            and doc.current_task_type == DocumentTaskType.DELETE_CLEANUP
            and doc.error_code == 20015
        )
