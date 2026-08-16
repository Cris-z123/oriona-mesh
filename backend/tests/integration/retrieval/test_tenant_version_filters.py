"""双路检索租户/版本/完成状态过滤失败测试（T061 / FR-012、FR-015、data-model.md 证据门槛）。

只通过统一 ``ChunkRepository`` 检索，强制过滤当前用户、知识库、``documents.status =
completed`` 与 ``chunks.document_version = documents.version``；低于
``RETRIEVAL_VECTOR_MIN_SIMILARITY``（0.65）或 ``RETRIEVAL_TRGM_MIN_SIMILARITY``（0.30）
的候选在 RRF 前被 SQL 门槛排除。需要真实 PostgreSQL（pgvector/pg_trgm）。
"""

import math
import uuid

import pytest
from sqlalchemy import func, select

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.enums import DocumentStatus, FileType
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User

pytestmark = pytest.mark.integration

_EMBEDDING_DIM = 1536
# 与 quickstart 检索配置契约一致（T026 默认值）。
_VECTOR_MIN = 0.65
_TRGM_MIN = 0.30


def _vector(*coeffs: float) -> list[float]:
    vec = [0.0] * _EMBEDDING_DIM
    for i, c in enumerate(coeffs):
        vec[i] = c
    return vec


def _user(db, email: str) -> User:
    user = User(email=email, password_hash="x" * 60)
    db.add(user)
    db.flush()
    return user


def _kb(db, user: User, name: str = "kb") -> KnowledgeBase:
    kb = KnowledgeBase(user_id=user.id, name=name)
    db.add(kb)
    db.flush()
    return kb


def _completed_doc(db, kb: KnowledgeBase, *, version: int = 1) -> Document:
    doc = Document(
        user_id=kb.user_id,
        knowledge_base_id=kb.id,
        filename=f"doc-{uuid.uuid4().hex[:8]}.txt",
        file_type=FileType.TXT,
        file_size=10,
        status=DocumentStatus.COMPLETED,
        version=version,
        storage_path="tmp/a.txt",
        upload_batch_id=uuid.uuid4(),
        content_hash="x" * 64,
        chunk_count=1,
    )
    db.add(doc)
    db.flush()
    return doc


def _chunk(
    db,
    doc: Document,
    *,
    seq: int,
    content: str,
    embedding: list[float],
    version: int | None = None,
) -> Chunk:
    chunk = Chunk(
        user_id=doc.user_id,
        knowledge_base_id=doc.knowledge_base_id,
        document_id=doc.id,
        document_version=version if version is not None else doc.version,
        seq=seq,
        content=content,
        embedding=embedding,
        embedding_model="text-embedding-3-small",
        policy_version="v1",
    )
    db.add(chunk)
    db.flush()
    return chunk


class TestTenantAndStatusFilters:
    def test_vector_and_keyword_search_are_scoped_to_user_and_kb(self, db_session) -> None:
        from app.repositories.chunks import ChunkRepository

        owner = _user(db_session, "retr-owner@example.com")
        kb = _kb(db_session, owner)
        doc = _completed_doc(db_session, kb)
        _chunk(db_session, doc, seq=0, content="hello world foo bar", embedding=_vector(1.0))
        other_user = _user(db_session, "retr-other@example.com")
        other_kb = _kb(db_session, other_user, name="other")
        other_doc = _completed_doc(db_session, other_kb)
        _chunk(
            db_session,
            other_doc,
            seq=0,
            content="secret other content",
            embedding=_vector(1.0),
        )
        db_session.commit()

        repo = ChunkRepository(db_session)
        # 其他用户、其他知识库均不可见。
        assert repo.vector_search(other_user.id, kb.id, _vector(1.0), _VECTOR_MIN) == []
        assert repo.vector_search(owner.id, other_kb.id, _vector(1.0), _VECTOR_MIN) == []
        assert repo.keyword_search(owner.id, other_kb.id, "hello world foo bar", _TRGM_MIN) == []
        # 属主自己的检索可见。
        hits = repo.vector_search(owner.id, kb.id, _vector(1.0), _VECTOR_MIN)
        assert len(hits) == 1
        assert hits[0].document_id == doc.id
        hits = repo.keyword_search(owner.id, kb.id, "hello world foo bar", _TRGM_MIN)
        assert len(hits) == 1
        assert hits[0].document_id == doc.id

    def test_old_version_chunks_are_excluded(self, db_session) -> None:
        from app.repositories.chunks import ChunkRepository

        user = _user(db_session, "retr-version@example.com")
        kb = _kb(db_session, user)
        doc = _completed_doc(db_session, kb, version=2)
        # 片段只存在于 v1，资料当前版本是 v2 → 不得检索。
        _chunk(
            db_session,
            doc,
            seq=0,
            content="old version content",
            embedding=_vector(1.0),
            version=1,
        )
        db_session.commit()
        repo = ChunkRepository(db_session)
        assert repo.vector_search(user.id, kb.id, _vector(1.0), _VECTOR_MIN) == []
        assert repo.keyword_search(user.id, kb.id, "old version content", _TRGM_MIN) == []

    def test_not_completed_documents_are_excluded(self, db_session) -> None:
        from app.repositories.chunks import ChunkRepository

        user = _user(db_session, "retr-status@example.com")
        kb = _kb(db_session, user)
        for status in (
            DocumentStatus.PENDING,
            DocumentStatus.QUEUED,
            DocumentStatus.PROCESSING,
            DocumentStatus.FAILED,
            DocumentStatus.DELETING,
            DocumentStatus.DELETED,
        ):
            doc = _completed_doc(db_session, kb)
            doc.status = status
            db_session.flush()
            _chunk(db_session, doc, seq=0, content="status content", embedding=_vector(1.0))
        db_session.commit()
        repo = ChunkRepository(db_session)
        assert repo.vector_search(user.id, kb.id, _vector(1.0), _VECTOR_MIN) == []
        assert repo.keyword_search(user.id, kb.id, "status content", _TRGM_MIN) == []

    def test_chunks_of_other_documents_in_same_kb_are_not_mixed(self, db_session) -> None:
        from app.repositories.chunks import ChunkRepository

        user = _user(db_session, "retr-kb-scope@example.com")
        kb = _kb(db_session, user)
        doc = _completed_doc(db_session, kb)
        _chunk(db_session, doc, seq=0, content="target content", embedding=_vector(1.0))
        db_session.commit()
        repo = ChunkRepository(db_session)
        # 检索永远按知识库整体召回，不按单资料过滤；此处验证没有跨库泄漏即可。
        hits = repo.vector_search(user.id, kb.id, _vector(1.0), _VECTOR_MIN)
        assert [h.document_id for h in hits] == [doc.id]


class TestEvidenceThresholds:
    def test_vector_candidates_below_threshold_excluded_before_rrf(self, db_session) -> None:
        from app.repositories.chunks import ChunkRepository

        user = _user(db_session, "retr-vec@example.com")
        kb = _kb(db_session, user)
        doc = _completed_doc(db_session, kb)
        # 查询向量 q=[1,0,...]；余弦相似度即首维数值。
        _chunk(db_session, doc, seq=0, content="high sim", embedding=_vector(0.8, 0.6))
        _chunk(
            db_session,
            doc,
            seq=1,
            content="low sim",
            embedding=_vector(0.5, math.sqrt(0.75)),
        )
        db_session.commit()
        repo = ChunkRepository(db_session)
        hits = repo.vector_search(user.id, kb.id, _vector(1.0), _VECTOR_MIN)
        assert [h.seq for h in hits] == [0]
        assert hits[0].vector_similarity is not None
        assert hits[0].vector_similarity >= _VECTOR_MIN
        # 低于门槛的候选携带的相似度被排除且不参与 RRF。
        assert not any(
            h.vector_similarity is not None and h.vector_similarity < _VECTOR_MIN for h in hits
        )

    def test_keyword_candidates_below_threshold_excluded_before_rrf(self, db_session) -> None:
        from app.repositories.chunks import ChunkRepository

        user = _user(db_session, "retr-trgm@example.com")
        kb = _kb(db_session, user)
        doc = _completed_doc(db_session, kb)
        exact = "the quick brown fox jumps over the lazy dog"
        unrelated = "zzzz yyyy xxxx wwww vvvv uuuu"
        _chunk(db_session, doc, seq=0, content=exact, embedding=_vector(0.0))
        _chunk(db_session, doc, seq=1, content=unrelated, embedding=_vector(0.0))
        db_session.commit()
        # 确保测试数据确实跨越 0.30 门槛（pg_trgm 对短串相似度偏高）。
        low = db_session.scalar(select(func.similarity(exact, unrelated)))
        assert low is not None and low < _TRGM_MIN, f"fixture trgm similarity {low} not < 0.30"
        repo = ChunkRepository(db_session)
        hits = repo.keyword_search(user.id, kb.id, exact, _TRGM_MIN)
        assert [h.seq for h in hits] == [0]
        assert hits[0].keyword_similarity is not None
        assert hits[0].keyword_similarity >= _TRGM_MIN

    def test_empty_after_threshold_filtering(self, db_session) -> None:
        from app.repositories.chunks import ChunkRepository

        user = _user(db_session, "retr-empty@example.com")
        kb = _kb(db_session, user)
        doc = _completed_doc(db_session, kb)
        # 与查询向量 [1,0,...] 正交（余弦 0）→ 低于 0.65 门槛被排除。
        _chunk(db_session, doc, seq=0, content="nothing here", embedding=_vector(0.0, 1.0))
        db_session.commit()
        repo = ChunkRepository(db_session)
        assert repo.vector_search(user.id, kb.id, _vector(1.0), _VECTOR_MIN) == []
        assert repo.keyword_search(user.id, kb.id, "completely unrelated query", _TRGM_MIN) == []
