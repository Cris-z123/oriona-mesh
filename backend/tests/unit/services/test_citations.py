"""引用功能测试（T075 / FR-016、openapi.yaml Citation、data-model.md 回答引用）。

覆盖：有证据回答保存字段完整的 Citation 草稿（rank 从 1 起、融合/重排分数、必填
快照字段与受限内容预览）；SSE 预览与引用详情复用同一字段语义；删除/不可访问来源
回退 snapshot（ID 置空、字段取自快照），当前可访问来源为 live（ID 必填、字段取自
当前 chunk/document）。需要真实 PostgreSQL 的部分标为 integration。
"""

import uuid

import pytest

from app.models.enums import DocumentStatus, FileType
from app.repositories.chunks import RetrievalChunk
from app.services.citation_service import (
    CitationService,
    build_citation_drafts,
    citation_preview_dtos,
)

pytestmark = pytest.mark.unit


def _chunk(seq: int = 0, *, content: str = "来源内容预览") -> RetrievalChunk:
    return RetrievalChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_version=1,
        seq=seq,
        content=content,
        page=2,
        section="结论",
        filename="source.txt",
        file_type="txt",
        vector_similarity=0.9,
        keyword_similarity=0.8,
        fused_score=0.92,
    )


class TestBuildCitationDrafts:
    def test_drafts_carry_rank_score_and_snapshot_fields(self) -> None:
        candidates = [_chunk(0), _chunk(1)]
        drafts = build_citation_drafts(candidates)
        assert [d.rank for d in drafts] == [1, 2]
        assert drafts[0].score == 0.92
        assert drafts[0].chunk_id == candidates[0].chunk_id
        assert drafts[0].document_id == candidates[0].document_id
        assert drafts[0].document_version == 1
        snapshot = drafts[0].snapshot
        assert snapshot["filename"] == "source.txt"
        assert snapshot["file_type"] == "txt"
        assert snapshot["page"] == 2
        assert snapshot["section"] == "结论"
        assert snapshot["content"] == "来源内容预览"

    def test_content_preview_is_truncated(self) -> None:
        long_content = "x" * 1000
        drafts = build_citation_drafts([_chunk(0, content=long_content)])
        assert len(drafts[0].snapshot["content"]) == 400

    def test_empty_candidates_produce_no_drafts(self) -> None:
        assert build_citation_drafts([]) == []


class TestCitationPreviewDtos:
    def test_preview_uses_same_citation_field_semantics(self) -> None:
        drafts = build_citation_drafts([_chunk(0)])
        preview = citation_preview_dtos(drafts)[0]
        for field in (
            "rank",
            "score",
            "chunk_id",
            "document_id",
            "document_version",
            "filename",
            "file_type",
            "page",
            "section",
            "content",
            "source_type",
        ):
            assert field in preview
        assert preview["source_type"] == "live"
        assert uuid.UUID(preview["chunk_id"])
        assert uuid.UUID(preview["document_id"])
        assert preview["filename"] == "source.txt"

    def test_empty_drafts_produce_empty_previews(self) -> None:
        assert citation_preview_dtos([]) == []


@pytest.mark.integration
class TestLiveSnapshotDiscrimination:
    """live/snapshot 判别（删除后快照回退）；需要真实 PostgreSQL。"""

    def _setup(self, db, user, kb) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        from app.models.chunk import Chunk
        from app.models.conversation import Conversation, Message, MessageCitation
        from app.models.document import Document
        from app.models.enums import MessageFinishReason, MessageRole, MessageStatus

        doc = Document(
            user_id=user.id,
            knowledge_base_id=kb.id,
            filename="source.txt",
            file_type=FileType.TXT,
            file_size=10,
            status=DocumentStatus.COMPLETED,
            version=1,
            storage_path="tmp/source.txt",
            upload_batch_id=uuid.uuid4(),
            content_hash="x" * 64,
            chunk_count=1,
        )
        db.add(doc)
        db.flush()
        chunk = Chunk(
            user_id=user.id,
            knowledge_base_id=kb.id,
            document_id=doc.id,
            document_version=1,
            seq=0,
            content="来源内容预览",
            embedding=[0.1] + [0.0] * 1535,
            embedding_model="text-embedding-3-small",
            policy_version="v1",
            page=2,
            section="结论",
        )
        db.add(chunk)
        db.flush()
        conv = Conversation(user_id=user.id, knowledge_base_id=kb.id)
        db.add(conv)
        db.flush()
        msg = Message(
            user_id=user.id,
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            finish_reason=MessageFinishReason.STOP,
            content="回答",
        )
        db.add(msg)
        db.flush()
        db.add(
            MessageCitation(
                message_id=msg.id,
                user_id=user.id,
                knowledge_base_id=kb.id,
                chunk_id=chunk.id,
                document_id=doc.id,
                document_version=1,
                rank=1,
                score=0.92,
                chunk_snapshot={
                    "filename": "stale.txt",
                    "file_type": "txt",
                    "page": 9,
                    "section": "旧章节",
                    "content": "旧内容",
                },
            )
        )
        db.commit()
        return conv.id, msg.id, doc.id, chunk.id

    def test_accessible_source_is_live_with_current_fields(self, db_session) -> None:
        from app.models.knowledge_base import KnowledgeBase
        from app.models.user import User

        user = User(email="cite-live@example.com", password_hash="x" * 60)
        db_session.add(user)
        db_session.flush()
        kb = KnowledgeBase(user_id=user.id, name="kb")
        db_session.add(kb)
        db_session.flush()
        conv_id, msg_id, _doc_id, chunk_id = self._setup(db_session, user, kb)

        items, total = CitationService(db_session).list_for_message(
            msg_id, conv_id, user.id, page=1, page_size=20
        )
        assert total == 1
        item = items[0]
        assert item["source_type"] == "live"
        assert uuid.UUID(item["chunk_id"]) == chunk_id
        assert item["filename"] == "source.txt"  # 字段取自当前来源而非快照
        assert item["page"] == 2
        assert item["content"] == "来源内容预览"

    def test_deleted_source_falls_back_to_snapshot_with_null_ids(self, db_session) -> None:
        from sqlalchemy import delete

        from app.models.chunk import Chunk
        from app.models.document import Document
        from app.models.knowledge_base import KnowledgeBase
        from app.models.user import User

        user = User(email="cite-snap@example.com", password_hash="x" * 60)
        db_session.add(user)
        db_session.flush()
        kb = KnowledgeBase(user_id=user.id, name="kb")
        db_session.add(kb)
        db_session.flush()
        conv_id, msg_id, doc_id, chunk_id = self._setup(db_session, user, kb)

        # 删除来源（资料删除编排完成后物理删除；引用外键 SET NULL）。
        db_session.execute(delete(Document).where(Document.id == doc_id))
        db_session.execute(delete(Chunk).where(Chunk.id == chunk_id))
        db_session.commit()

        items, _total = CitationService(db_session).list_for_message(
            msg_id, conv_id, user.id, page=1, page_size=20
        )
        item = items[0]
        assert item["source_type"] == "snapshot"
        assert item["chunk_id"] is None
        assert item["document_id"] is None
        assert item["document_version"] == 1
        # 展示字段来自保存快照，不恢复原始资料。
        assert item["filename"] == "stale.txt"
        assert item["page"] == 9
        assert item["section"] == "旧章节"
        assert item["content"] == "旧内容"
