"""统一片段仓储（T020 / data-model.md 片段读取边界）。

``chunks`` 的所有读取必须经过本仓储；除迁移与测试夹具外，路由、服务与 worker 不得
直接执行该表 SQL 或 ORM 查询。

- 检索方法固定过滤当前用户、知识库、``documents.status = completed`` 与
  ``chunks.document_version = documents.version``（当前可检索资料定义）；
- 流水线内部方法固定过滤当前用户、知识库、资料与精确版本，不得复用于用户查询；
- 证据门槛（向量/关键词相似度）与相似度检索自 T067/T068 提供。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.enums import DocumentStatus


class ChunkRepository:
    """统一片段仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # 检索方法（当前用户、知识库、完成态、当前版本）
    # ------------------------------------------------------------------
    def count_retrievable(self, user_id: uuid.UUID, knowledge_base_id: uuid.UUID) -> int:
        """当前可检索（已发布且为当前版本）片段数量；供就绪与诊断使用。"""
        return int(
            self.session.scalar(
                select(func.count(Chunk.id))
                .select_from(Chunk)
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Chunk.user_id == user_id,
                    Chunk.knowledge_base_id == knowledge_base_id,
                    Document.status == DocumentStatus.COMPLETED,
                    Chunk.document_version == Document.version,
                )
            )
            or 0
        )

    def list_retrievable_count_by_document(
        self, user_id: uuid.UUID, knowledge_base_id: uuid.UUID
    ) -> dict[uuid.UUID, int]:
        """按资料统计当前可检索片段数（检索边界与 count_retrievable 一致）。"""
        rows = self.session.execute(
            select(Chunk.document_id, func.count(Chunk.id))
            .select_from(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.user_id == user_id,
                Chunk.knowledge_base_id == knowledge_base_id,
                Document.status == DocumentStatus.COMPLETED,
                Chunk.document_version == Document.version,
            )
            .group_by(Chunk.document_id)
        ).all()
        return {doc_id: int(count) for doc_id, count in rows}

    # ------------------------------------------------------------------
    # 流水线方法（当前用户、知识库、资料、精确版本）
    # ------------------------------------------------------------------
    def count_for_pipeline(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
    ) -> int:
        """精确版本正式片段计数（finalize 校验与诊断使用，不得用于用户查询）。"""
        return int(
            self.session.scalar(
                select(func.count(Chunk.id)).where(
                    Chunk.user_id == user_id,
                    Chunk.knowledge_base_id == knowledge_base_id,
                    Chunk.document_id == document_id,
                    Chunk.document_version == document_version,
                )
            )
            or 0
        )

    def insert(self, chunk: Chunk) -> None:
        """流水线直写正式片段（Phase 3 起由 fencing 事务调用）。"""
        self.session.add(chunk)
