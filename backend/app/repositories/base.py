"""以 ``user_id`` 为强制范围的资源仓储基类（T020 / FR-020）。

- 按 ID 查询必须同时过滤当前用户；未命中时直接抛出对应 ``404`` 业务错误，不得做
  任何全局存在性探测（不得区分“不存在”与“属于其他用户”）；
- 知识库映射 ``20002/404``，其他租户资源统一映射 ``20007/404``；
- 仓储只接收 ``Session`` 与当前 ``user_id``，绝不接收未校验的租户边界。
"""

import uuid
from datetime import datetime
from typing import Protocol, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import (
    KNOWLEDGE_BASE_NOT_FOUND_MSG,
    RESOURCE_NOT_FOUND_MSG,
)
from app.models.knowledge_base import KnowledgeBase


class _ScopedModel(Protocol):
    """租户资源模型的最小属性协议（id/user_id/created_at）。"""

    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime


M = TypeVar("M", bound=_ScopedModel)


class TenantScopedRepository[M]:
    """带当前用户边界的通用资源仓储。"""

    def __init__(self, session: Session, model: type[M], not_found_code: int = 20007) -> None:
        self.session = session
        self._model = model
        self._not_found_code = not_found_code

    def get_for_user(self, entity_id: uuid.UUID, user_id: uuid.UUID) -> M:
        """按 ID 在当前用户范围内读取；未命中抛 ``{not_found_code}/404``（不全局探测）。"""
        row = self.session.scalar(
            select(self._model).where(
                self._model.id == entity_id,  # type: ignore[attr-defined]
                self._model.user_id == user_id,  # type: ignore[attr-defined]
            )
        )
        if row is None:
            raise ApiError(self._not_found_code, RESOURCE_NOT_FOUND_MSG, 404)
        return row

    def list_for_user(
        self, user_id: uuid.UUID, *, page: int, page_size: int
    ) -> tuple[list[M], int]:
        """按当前用户分页读取（列表过滤条件由子类扩展）。"""
        total = self.session.scalar(
            select(func.count()).select_from(self._model).where(self._model.user_id == user_id)  # type: ignore[attr-defined]
        )
        rows = self.session.scalars(
            select(self._model)
            .where(self._model.user_id == user_id)  # type: ignore[attr-defined]
            .order_by(self._model.created_at.desc())  # type: ignore[attr-defined]
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(rows), int(total or 0)


def require_knowledge_base(session: Session, kb_id: uuid.UUID, user_id: uuid.UUID) -> KnowledgeBase:
    """在当前用户范围内读取知识库；未命中统一 ``20002/404``，禁止全局探测。"""
    kb = session.scalar(
        select(KnowledgeBase).where(KnowledgeBase.id == kb_id, KnowledgeBase.user_id == user_id)
    )
    if kb is None:
        raise ApiError(20002, KNOWLEDGE_BASE_NOT_FOUND_MSG, 404)
    return kb
