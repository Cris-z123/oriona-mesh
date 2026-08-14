"""知识库服务（T023 / FR-003）。

- 创建、查询、更新与空知识库的同步删除；
- 非空知识库的 ``deleting`` 编排（有界停止、delete_cleanup、失败墓碑）依赖资料
  删除能力，由 T081 实现；本阶段对非空知识库返回 ``20008/409``；
- 列表与详情以所属用户为范围；``deleting`` 从普通读取隐藏；``delete_failed`` 最小
  墓碑由 T081 收敛（DTO 已支持）。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import (
    KNOWLEDGE_BASE_NOT_FOUND_MSG,
    RESOURCE_CONFLICT_MSG,
)
from app.models.document import Document
from app.models.enums import DocumentStatus, KnowledgeBaseStatus
from app.models.knowledge_base import KnowledgeBase
from app.repositories.base import require_knowledge_base

_CONFLICT_STATUS = 409
_CONFLICT_CODE = 20008


class KnowledgeBaseService:
    """知识库 CRUD。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self, user_id: uuid.UUID, name: str, description: str | None = None
    ) -> KnowledgeBase:
        kb = KnowledgeBase(user_id=user_id, name=name, description=description)
        self.session.add(kb)
        self.session.commit()
        return kb

    def list(
        self, user_id: uuid.UUID, *, page: int, page_size: int
    ) -> tuple[list[KnowledgeBase], int]:
        """分页列出当前用户知识库；内部 deleting 始终隐藏。"""
        base = select(KnowledgeBase).where(
            KnowledgeBase.user_id == user_id,
            KnowledgeBase.status != KnowledgeBaseStatus.DELETING,
        )
        total = self.session.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = self.session.scalars(
            base.order_by(KnowledgeBase.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(rows), int(total)

    def get(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBase:
        """当前用户范围内读取；deleting/不存在统一 ``20002/404``（不全局探测）。"""
        kb = require_knowledge_base(self.session, kb_id, user_id)
        if kb.status == KnowledgeBaseStatus.DELETING:
            raise ApiError(20002, KNOWLEDGE_BASE_NOT_FOUND_MSG, 404)
        return kb

    def update(
        self,
        user_id: uuid.UUID,
        kb_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> KnowledgeBase:
        kb = self.get(user_id, kb_id)
        if kb.status == KnowledgeBaseStatus.DELETE_FAILED:
            # 删除未完成墓碑不可编辑（T025：delete_failed 禁止 PATCH）。
            raise ApiError(_CONFLICT_CODE, RESOURCE_CONFLICT_MSG, _CONFLICT_STATUS)
        if name is not None:
            kb.name = name
        if description is not None:
            kb.description = description
        self.session.commit()
        return kb

    def delete(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> None:
        """删除知识库。

        空知识库同步删除；非空知识库的 deleting 编排（T081）在本阶段未实现，返回
        ``20008/409`` 避免半删除状态。
        """
        kb = self._get_for_delete(user_id, kb_id)
        if kb.status == KnowledgeBaseStatus.DELETE_FAILED:
            raise ApiError(_CONFLICT_CODE, RESOURCE_CONFLICT_MSG, _CONFLICT_STATUS)
        if kb.status == KnowledgeBaseStatus.DELETING:
            # 命中 deleting 幂等成功（编排由 T081 接入扫描器）。
            return
        has_documents = (
            self.session.scalar(
                select(Document.id)
                .where(
                    Document.knowledge_base_id == kb_id,
                    Document.user_id == user_id,
                    Document.status != DocumentStatus.DELETED,
                )
                .limit(1)
            )
            is not None
        )
        if has_documents:
            raise ApiError(_CONFLICT_CODE, RESOURCE_CONFLICT_MSG, _CONFLICT_STATUS)
        self.session.delete(kb)
        self.session.commit()

    def _get_for_delete(self, user_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBase:
        """仅 DELETE 使用的锁定变更查询；可命中 active/deleting/delete_failed。"""
        kb = self.session.scalar(
            select(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id, KnowledgeBase.user_id == user_id)
            .with_for_update()
        )
        if kb is None:
            raise ApiError(20002, KNOWLEDGE_BASE_NOT_FOUND_MSG, 404)
        return kb
