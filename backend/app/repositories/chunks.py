"""统一片段仓储（T020/T054/T067/T068 / data-model.md 片段读取边界）。

``chunks`` 的所有读取必须经过本仓储；除迁移与测试夹具外，路由、服务与 worker 不得
直接执行该表 SQL 或 ORM 查询。

- 检索方法固定过滤当前用户、知识库、``documents.status = completed`` 与
  ``chunks.document_version = documents.version``（当前可检索资料定义）；
- 双路召回（T067 向量 / T068 pg_trgm 关键词）在 SQL 中应用证据门槛：
  ``RETRIEVAL_VECTOR_MIN_SIMILARITY``（余弦）与 ``RETRIEVAL_TRGM_MIN_SIMILARITY``，
  低于门槛的候选在 RRF 前排除；
- 流水线内部方法固定过滤当前用户、知识库、资料与精确版本，不得复用于用户查询；
- 正式片段写入（embed 直写）必须携带 ``attempt_id`` 并在同一事务通过 fencing 校验；
- 删除清理经 ``delete_for_document`` 整份移除（不参与任何读取路径）。
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.enums import DocumentStatus
from app.repositories.fencing import validate_attempt_write


@dataclass
class RetrievalChunk:
    """检索候选（已通过证据门槛，RRF/重排/上下文打包的输入）。

    由 ``vector_search``/``keyword_search`` 返回；``filename``/``file_type`` 来自
    documents JOIN，供引用快照使用。``fused_score`` 由检索服务在 RRF/重排后填充。
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_version: int
    seq: int
    content: str
    page: int | None = None
    section: str | None = None
    filename: str | None = None
    file_type: str | None = None
    vector_similarity: float | None = None
    keyword_similarity: float | None = None
    fused_score: float | None = field(default=None, compare=False)


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

    def vector_search(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        query_vector: list[float],
        min_similarity: float,
        *,
        limit: int = 20,
    ) -> list[RetrievalChunk]:
        """向量召回（T067）：余弦相似度在 SQL 中应用门槛，低于门槛不进入 RRF。"""
        similarity = 1 - Chunk.embedding.cosine_distance(query_vector)
        return self._search(
            user_id,
            knowledge_base_id,
            similarity,
            min_similarity,
            limit=limit,
            vector_similarity=True,
        )

    def keyword_search(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        query: str,
        min_similarity: float,
        *,
        limit: int = 20,
    ) -> list[RetrievalChunk]:
        """pg_trgm 关键词召回（T068）：相似度在 SQL 中应用门槛，低于门槛不进入 RRF。"""
        similarity = func.similarity(Chunk.content, query)
        return self._search(
            user_id,
            knowledge_base_id,
            similarity,
            min_similarity,
            limit=limit,
            keyword_similarity=True,
        )

    def _search(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        similarity,
        min_similarity: float,
        *,
        limit: int,
        vector_similarity: bool = False,
        keyword_similarity: bool = False,
    ) -> list[RetrievalChunk]:
        """双路召回共享实现：同一租户/版本/完成状态过滤构造器 + SQL 证据门槛。"""
        rows = self.session.execute(
            select(Chunk, Document, similarity.label("similarity"))
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.user_id == user_id,
                Chunk.knowledge_base_id == knowledge_base_id,
                Document.status == DocumentStatus.COMPLETED,
                Chunk.document_version == Document.version,
                similarity >= min_similarity,
            )
            .order_by(similarity.desc(), Chunk.seq.asc())
            .limit(limit)
        ).all()
        return [
            self._candidate(
                chunk,
                doc,
                vector_similarity=float(sim) if vector_similarity else None,
                keyword_similarity=float(sim) if keyword_similarity else None,
            )
            for chunk, doc, sim in rows
        ]

    def get_live_source(
        self,
        chunk_id: uuid.UUID,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[Chunk, Document] | None:
        """引用活表读取（T072）：当前可访问来源的 chunk/document（用户范围）。

        data-model.md 要求引用活表读取统一收口到本仓储；来源不可访问（资料已删除
        或行缺失）时返回 None，由 CitationService 回退保存的快照。
        """
        row = self.session.execute(
            select(Chunk, Document)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.id == chunk_id,
                Chunk.document_id == document_id,
                Chunk.user_id == user_id,
            )
        ).first()
        if row is None:
            return None
        return row[0], row[1]

    @staticmethod
    def _candidate(
        chunk: Chunk,
        doc: Document,
        *,
        vector_similarity: float | None = None,
        keyword_similarity: float | None = None,
    ) -> RetrievalChunk:
        return RetrievalChunk(
            chunk_id=chunk.id,
            document_id=doc.id,
            document_version=chunk.document_version,
            seq=chunk.seq,
            content=chunk.content,
            page=chunk.page,
            section=chunk.section,
            filename=doc.filename,
            file_type=doc.file_type.value if doc.file_type else None,
            vector_similarity=(
                round(float(vector_similarity), 6) if vector_similarity is not None else None
            ),
            keyword_similarity=(
                round(float(keyword_similarity), 6) if keyword_similarity is not None else None
            ),
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

    # ------------------------------------------------------------------
    # 流水线写入（T054：embed 直写，attempt_id fencing）
    # ------------------------------------------------------------------
    def replace_for_version(
        self,
        *,
        attempt_id: uuid.UUID,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
        chunks: list[Chunk],
    ) -> None:
        """attempt_id fencing 事务按唯一逻辑键幂等直写正式片段（重试安全）。"""
        validate_attempt_write(
            self.session,
            attempt_id=attempt_id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
        )
        self.session.execute(
            delete(Chunk).where(
                Chunk.user_id == user_id,
                Chunk.knowledge_base_id == knowledge_base_id,
                Chunk.document_id == document_id,
                Chunk.document_version == document_version,
            )
        )
        self.session.add_all(chunks)
        self.session.flush()

    def list_for_pipeline(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
    ) -> list[Chunk]:
        """精确版本正式片段（embed 重试安全检查与诊断使用，不得用于用户查询）。"""
        return list(
            self.session.scalars(
                select(Chunk)
                .where(
                    Chunk.user_id == user_id,
                    Chunk.knowledge_base_id == knowledge_base_id,
                    Chunk.document_id == document_id,
                    Chunk.document_version == document_version,
                )
                .order_by(Chunk.seq.asc())
            )
        )

    def delete_for_document(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> int:
        """删除清理：整份移除资料全部正式片段（不参与读取路径）。"""
        result = self.session.execute(
            delete(Chunk).where(
                Chunk.user_id == user_id,
                Chunk.knowledge_base_id == knowledge_base_id,
                Chunk.document_id == document_id,
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
