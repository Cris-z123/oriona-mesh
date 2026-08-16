"""知识库服务（T023/T081 / FR-003、data-model.md 删除知识库）。

- 创建、查询、更新与空知识库的同步物理删除；
- 非空知识库删除（T081）：DELETE 事务先置 ``deleting`` 并为其每份资料执行
  ``DocumentDeletionService`` 的删除编排（有界停止、delete_cleanup、名额释放与
  引用快照），提交后知识库及子资源对普通读取立即隐藏；
- 命中 ``deleting`` 幂等成功且不创建任务；任一子资料清理耗尽后由维护扫描器
  置 ``delete_failed/20015`` 最小墓碑；从 ``delete_failed`` 再次 DELETE 才转回
  ``deleting`` 并仅为失败子资料新建删除轮次；全部子资料 ``deleted`` 且无活动
  attempt 后由扫描器物理删除并级联对话、消息和引用；
- 列表与详情以所属用户为范围：``active`` 返回完整对象，``delete_failed`` 返回
  最小墓碑，``deleting`` 从普通读取隐藏。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import (
    KNOWLEDGE_BASE_NOT_FOUND_MSG,
    RESOURCE_CONFLICT_MSG,
)
from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.enums import DocumentStatus, KnowledgeBaseStatus
from app.models.knowledge_base import KnowledgeBase
from app.repositories.base import require_knowledge_base
from app.repositories.knowledge_bases import KnowledgeBaseRepository
from app.services.document_deletion_service import DocumentDeletionService
from app.workers.base import dispatch_task as _default_dispatch

_CONFLICT_STATUS = 409
_CONFLICT_CODE = 20008


class KnowledgeBaseService:
    """知识库 CRUD 与删除编排。"""

    def __init__(
        self,
        session: Session,
        dispatch: Callable[[str, tuple], None] | None = None,
    ) -> None:
        self.session = session
        self.knowledge_bases = KnowledgeBaseRepository(session)
        self.deleter = DocumentDeletionService(session, dispatch=dispatch or _default_dispatch)

    def create(
        self, user_id: uuid.UUID, name: str, description: str | None = None
    ) -> KnowledgeBase:
        kb = KnowledgeBase(user_id=user_id, name=name, description=description)
        self.session.add(kb)
        self.session.commit()
        return kb

    def list_for_user(
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
        """删除知识库（T081 编排）。

        空知识库在删除事务内直接物理删除；非空知识库置 ``deleting`` 并编排全部
        资料的删除，提交后立即隐藏。命中 ``deleting`` 幂等成功；从
        ``delete_failed`` 重试才转回 ``deleting`` 并仅为失败子资料新建轮次。
        """
        kb = self.knowledge_bases.lock_for_delete(kb_id, user_id)
        now = datetime.now(UTC)
        if kb.status == KnowledgeBaseStatus.DELETING:
            return  # 幂等成功：不创建任务
        docs = self._deletable_documents(kb)
        if kb.status == KnowledgeBaseStatus.DELETE_FAILED:
            # 重试：转回 deleting，仅为失败子资料创建新的删除轮次。
            kb.status = KnowledgeBaseStatus.DELETING
            kb.delete_error_code = None
            self._orchestrate_documents(user_id, docs, now, only_failed=True)
            return
        # active：空知识库立即物理删除；非空则编排全部子资料删除。
        if not docs:
            self.session.delete(kb)
            self.session.commit()
            return
        kb.status = KnowledgeBaseStatus.DELETING
        self._orchestrate_documents(user_id, docs, now, only_failed=False)

    def _orchestrate_documents(
        self,
        user_id: uuid.UUID,
        docs: list[Document],
        now: datetime,
        *,
        only_failed: bool,
    ) -> None:
        """对选中资料执行删除编排并提交；提交后才投递清理任务。

        ``only_failed=True``：知识库 ``delete_failed`` 重试时只为
        ``failed/delete_cleanup/20015`` 子资料新建轮次，其他子资料保持现状。
        """
        to_dispatch: list[DocumentTask] = []
        for doc in docs:
            if only_failed and not doc.is_delete_cleanup_failed:
                continue
            cleanup = self.deleter.stage_document_delete(doc, user_id, now)
            if cleanup is not None:
                to_dispatch.append(cleanup)
        self.session.commit()
        for cleanup in to_dispatch:
            self.deleter.dispatch_delete_cleanup(cleanup.id)

    def _deletable_documents(self, kb: KnowledgeBase) -> list[Document]:
        """知识库内尚未物理删除（status != deleted）的资料。

        与知识库行同事务锁定（FOR UPDATE），避免与并发资料 DELETE 竞争导致
        重复清理投递或删除轮次回退（review 修复）。
        """
        rows = self.session.scalars(
            select(Document)
            .where(
                Document.knowledge_base_id == kb.id,
                Document.user_id == kb.user_id,
                Document.status != DocumentStatus.DELETED,
            )
            .order_by(Document.id)
            .with_for_update()
        ).all()
        return list(rows)
