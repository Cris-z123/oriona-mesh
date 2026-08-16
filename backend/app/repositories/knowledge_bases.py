"""知识库仓储（T081 / FR-020、data-model.md 删除知识库）。

- ``lock_for_delete`` 仅供知识库 DELETE 使用，强制 ``user_id`` 且可锁定命中
  ``active``/``deleting``/``delete_failed``，禁止复用于 GET/list/子资源读取；
  ``delete_failed`` 最小墓碑的列表/详情读取经服务层 ``knowledge_base_dto``
  命中，普通内容读取只允许 ``active``（``require_active_knowledge_base``）；
- 普通读取（list/get/update）以 ``user_id`` 为范围，未命中统一 ``20002/404``，
  禁止全局存在性探测。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import KNOWLEDGE_BASE_NOT_FOUND_MSG
from app.models.knowledge_base import KnowledgeBase


class KnowledgeBaseRepository:
    """知识库仓储（租户范围固定当前用户）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_for_delete(self, kb_id: uuid.UUID, user_id: uuid.UUID) -> KnowledgeBase:
        """仅 DELETE 使用的锁定变更查询；命中 active/deleting/delete_failed。

        不存在或已物理删除统一 ``20002/404``；不得复用于普通读取。
        """
        kb = self.session.scalar(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.user_id == user_id)
            .with_for_update()
        )
        if kb is None:
            raise ApiError(20002, KNOWLEDGE_BASE_NOT_FOUND_MSG, 404)
        return kb
