"""统一 Citation DTO 服务（T072 / FR-016、openapi.yaml Citation）。

- 回答完成时按最终排序保存引用草稿：``chunk_id``/``document_id`` 指向当前可访问
  来源，`chunk_snapshot` 保存文件名、类型、定位与受限内容预览；
- 读取时按当前来源可访问性区分 ``live``（两个 ID 必填，字段取当前
  chunk/document）与 ``snapshot``（资料已删除或不可访问：ID 置空，字段取自
  保存的快照）；快照只供历史核验，不可恢复原始资料；
- 引用数组按 ``rank`` 升序返回；同一消息内 rank 唯一（数据库约束）。
"""

import uuid

from sqlalchemy.orm import Session

from app.models.conversation import MessageCitation
from app.repositories.chunks import ChunkRepository, RetrievalChunk
from app.repositories.conversations import CitationDraft, ConversationRepository

# 内容预览上限（受限长度，不得用于恢复原始资料）。
_PREVIEW_MAX_CHARS = 400


def build_citation_drafts(candidates: list[RetrievalChunk]) -> list[CitationDraft]:
    """按最终排序（候选顺序）生成引用草稿；rank 从 1 开始。"""
    drafts: list[CitationDraft] = []
    for rank, candidate in enumerate(candidates, start=1):
        snapshot = {
            "filename": candidate.filename,
            "file_type": candidate.file_type,
            "page": candidate.page,
            "section": candidate.section,
            "content": candidate.content[:_PREVIEW_MAX_CHARS],
        }
        drafts.append(
            CitationDraft(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                document_version=candidate.document_version,
                rank=rank,
                score=candidate.fused_score if candidate.fused_score is not None else 0.0,
                snapshot=snapshot,
            )
        )
    return drafts


class CitationService:
    """引用保存与统一 Citation DTO 读取。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = ConversationRepository(session)
        self.chunk_repository = ChunkRepository(session)

    # ------------------------------------------------------------------
    # AnswerService 引用端口
    # ------------------------------------------------------------------
    def save(
        self,
        *,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        drafts: list[CitationDraft],
    ) -> None:
        """保存回答引用（答案完成时调用；空列表为无证据路径的零写入）。"""
        self.repository.save_citations(message_id, user_id, knowledge_base_id, drafts)
        self.session.commit()

    # ------------------------------------------------------------------
    # 读取（live/snapshot 判别）
    # ------------------------------------------------------------------
    def list_for_message(
        self,
        message_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[dict], int]:
        """按 rank 升序返回统一 Citation DTO；先校验消息与对话归属。"""
        rows, total = self.repository.list_citations(
            message_id, conversation_id, user_id, page=page, page_size=page_size
        )
        return [self._dto(row) for row in rows], total

    def _dto(self, row: MessageCitation) -> dict:
        live = None
        if row.chunk_id is not None and row.document_id is not None:
            # 引用活表读取统一经 ChunkRepository（用户范围，data-model.md 片段读取边界）。
            live = self.chunk_repository.get_live_source(row.chunk_id, row.document_id, row.user_id)
        if live is not None:
            live_chunk, live_document = live
            return {
                "rank": row.rank,
                "score": row.score,
                "chunk_id": str(row.chunk_id),
                "document_id": str(row.document_id),
                "document_version": row.document_version,
                "filename": live_document.filename,
                "file_type": live_document.file_type.value,
                "page": live_chunk.page,
                "section": live_chunk.section,
                "content": live_chunk.content[:_PREVIEW_MAX_CHARS],
                "source_type": "live",
            }
        snapshot = row.chunk_snapshot or {}
        return {
            "rank": row.rank,
            "score": row.score,
            "chunk_id": None,
            "document_id": None,
            "document_version": row.document_version,
            "filename": snapshot.get("filename"),
            "file_type": snapshot.get("file_type"),
            "page": snapshot.get("page"),
            "section": snapshot.get("section"),
            "content": snapshot.get("content"),
            "source_type": "snapshot",
        }


def citation_preview_dtos(drafts: list[CitationDraft]) -> list[dict]:
    """SSE ``retrieval_done`` 的引用预览：与引用详情复用同一字段语义。"""
    return [
        {
            "rank": draft.rank,
            "score": draft.score,
            "chunk_id": str(draft.chunk_id) if draft.chunk_id else None,
            "document_id": str(draft.document_id) if draft.document_id else None,
            "document_version": draft.document_version,
            "filename": draft.snapshot.get("filename"),
            "file_type": draft.snapshot.get("file_type"),
            "page": draft.snapshot.get("page"),
            "section": draft.snapshot.get("section"),
            "content": draft.snapshot.get("content"),
            "source_type": "live",
        }
        for draft in drafts
    ]
