"""统一片段仓储集成测试（T021 / data-model.md 片段读取边界）。

覆盖：检索方法固定过滤用户/知识库/完成态/当前版本（未 finalize 与旧版本片段
排除）；流水线方法固定过滤用户/知识库/资料/精确版本（不混用）；跨用户隔离。
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.enums import DocumentStatus, FileType
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.repositories.chunks import ChunkRepository

pytestmark = pytest.mark.integration

_MODEL = "text-embedding-3-small"
_POLICY = "v1"


@pytest.fixture()
def chunk_fixture(db_session: Session):
    user = User(email="owner@example.com", password_hash="h")
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db_session.add(kb)
    db_session.flush()

    def _document(status: DocumentStatus, version: int = 1) -> Document:
        doc = Document(
            user_id=user.id,
            knowledge_base_id=kb.id,
            filename="d.pdf",
            file_type=FileType.PDF,
            file_size=10,
            storage_path="o/d",
            upload_batch_id=uuid.uuid4(),
            content_hash="c",
            status=status,
            version=version,
        )
        db_session.add(doc)
        db_session.flush()
        return doc

    def _chunk(document: Document, seq: int, version: int | None = None) -> Chunk:
        chunk = Chunk(
            user_id=user.id,
            knowledge_base_id=kb.id,
            document_id=document.id,
            document_version=version if version is not None else document.version,
            seq=seq,
            content=f"chunk-{seq}",
            embedding=[0.1] * 1536,  # 与 Vector(1536) 列维度一致
            embedding_model=_MODEL,
            policy_version=_POLICY,
        )
        db_session.add(chunk)
        db_session.flush()
        return chunk

    published = _document(DocumentStatus.COMPLETED)
    not_finalized = _document(DocumentStatus.PROCESSING)
    stale = _document(DocumentStatus.COMPLETED, version=2)

    _chunk(published, 0)
    _chunk(not_finalized, 0)  # 未 finalize：不得参与检索
    _chunk(stale, 0, version=1)  # 旧版本：不得参与检索
    db_session.commit()
    return {
        "user": user,
        "kb": kb,
        "published": published,
        "not_finalized": not_finalized,
        "stale": stale,
    }


class TestRetrievalFilters:
    def test_unfinalized_and_stale_chunks_excluded_from_retrieval(
        self, db_session: Session, chunk_fixture
    ) -> None:
        repo = ChunkRepository(db_session)
        user, kb = chunk_fixture["user"], chunk_fixture["kb"]
        # 三份资料共 3 个片段，但只有已发布且当前版本的那 1 个可检索。
        assert repo.count_retrievable(user.id, kb.id) == 1

    def test_pipeline_count_uses_exact_version(self, db_session: Session, chunk_fixture) -> None:
        repo = ChunkRepository(db_session)
        user, kb = chunk_fixture["user"], chunk_fixture["kb"]
        # 流水线内部方法按精确版本计数：未发布片段也计入（供 finalize 校验）。
        assert repo.count_for_pipeline(user.id, kb.id, chunk_fixture["published"].id, 1) == 1
        assert repo.count_for_pipeline(user.id, kb.id, chunk_fixture["not_finalized"].id, 1) == 1
        # 旧版本片段以精确版本 1 计数，不因资料当前版本 2 而丢失。
        assert repo.count_for_pipeline(user.id, kb.id, chunk_fixture["stale"].id, 1) == 1
        assert repo.count_for_pipeline(user.id, kb.id, chunk_fixture["stale"].id, 2) == 0

    def test_pipeline_methods_not_usable_for_user_queries(
        self, db_session: Session, chunk_fixture
    ) -> None:
        # 流水线精确版本计数必须过滤用户/知识库：其他用户范围恒为 0。
        repo = ChunkRepository(db_session)
        other = User(email="other@example.com", password_hash="h")
        db_session.add(other)
        db_session.flush()
        published = chunk_fixture["published"]
        assert repo.count_for_pipeline(other.id, chunk_fixture["kb"].id, published.id, 1) == 0
        assert repo.count_retrievable(other.id, chunk_fixture["kb"].id) == 0


class TestRetrievalScope:
    def test_cross_user_retrieval_excluded(self, db_session: Session, chunk_fixture) -> None:
        repo = ChunkRepository(db_session)
        other_kb = KnowledgeBase(user_id=chunk_fixture["user"].id, name="other")
        db_session.add(other_kb)
        db_session.commit()
        # 其他知识库范围：资料不属于该库，检索恒为 0。
        assert repo.count_retrievable(chunk_fixture["user"].id, other_kb.id) == 0

    def test_retrievable_count_by_document(self, db_session: Session, chunk_fixture) -> None:
        repo = ChunkRepository(db_session)
        counts = repo.list_retrievable_count_by_document(
            chunk_fixture["user"].id, chunk_fixture["kb"].id
        )
        assert counts == {chunk_fixture["published"].id: 1}
