"""检索融合、重排与上下文打包失败测试（T061 / T069 / plan.md 决策 4）。

覆盖：RRF 融合只消费通过门槛的候选、跨路候选加权、确定性排序；合法 reranker 评分
按 score 降序且同分保持 RRF 原顺序；缺失/重复/越界/非有限评分整体回退原 RRF 顺序；
3000 token 上下文打包与相邻片段去重；融合为空时返回空结果（由 T062 收敛为可信拒答）。
"""

import uuid

import pytest

from app.infrastructure.model_gateway.types import RerankScore
from app.repositories.chunks import RetrievalChunk
from app.services.retrieval_service import (
    apply_rerank,
    estimate_tokens,
    pack_context,
    rrf_fuse,
)

pytestmark = pytest.mark.unit


def _chunk(
    *,
    seq: int = 0,
    document_id: str = "d",
    content: str = "c",
    vector_similarity: float | None = None,
    keyword_similarity: float | None = None,
    page: int | None = None,
    section: str | None = None,
) -> RetrievalChunk:
    return RetrievalChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.UUID(int=1) if document_id == "d" else uuid.uuid4(),
        document_version=1,
        seq=seq,
        content=content,
        page=page,
        section=section,
        vector_similarity=vector_similarity,
        keyword_similarity=keyword_similarity,
    )


class TestRrfFusion:
    def test_candidate_in_both_lists_ranks_above_single_source(self) -> None:
        both = _chunk(seq=0, vector_similarity=0.9, keyword_similarity=0.9)
        only_vector = _chunk(seq=1, vector_similarity=0.9)
        only_keyword = _chunk(seq=2, keyword_similarity=0.9)
        fused = rrf_fuse([both, only_vector], [both, only_keyword])
        assert [c.seq for c in fused] == [0, 1, 2]

    def test_rank_order_deterministic_with_single_list(self) -> None:
        a = _chunk(seq=0, vector_similarity=0.9)
        b = _chunk(seq=1, vector_similarity=0.8)
        c = _chunk(seq=2, vector_similarity=0.7)
        fused = rrf_fuse([a, b, c], [])
        assert [x.seq for x in fused] == [0, 1, 2]

    def test_fused_score_prefers_cross_source_ranks(self) -> None:
        # 一路排名第 1 的候选 vs 两路排名第 2 的候选：RRF 融合后两路候选更高。
        cross = _chunk(seq=0, vector_similarity=0.8, keyword_similarity=0.8)
        top = _chunk(seq=1, vector_similarity=0.99)
        fused = rrf_fuse([top, cross], [cross])
        assert [c.seq for c in fused] == [0, 1]

    def test_fused_score_is_set_and_strictly_positive(self) -> None:
        a = _chunk(seq=0, vector_similarity=0.9)
        b = _chunk(seq=1, keyword_similarity=0.7)
        fused = rrf_fuse([a], [b])
        assert len(fused) == 2
        for c in fused:
            assert c.fused_score is not None and c.fused_score > 0
        first, second = fused[0].fused_score, fused[1].fused_score
        assert first is not None and second is not None
        assert first >= second

    def test_empty_inputs_produce_empty_result(self) -> None:
        assert rrf_fuse([], []) == []

    def test_equal_fused_scores_break_ties_by_document_id_then_seq(self) -> None:
        # a: 向量第 1 + 关键词第 2；b: 向量第 2 + 关键词第 1 → 融合分相等。
        # 同分按 (document_id, seq) 升序：doc_a < doc_b 时 a 在前。
        doc_a, doc_b = uuid.UUID(int=1), uuid.UUID(int=2)
        a = RetrievalChunk(
            chunk_id=uuid.uuid4(),
            document_id=doc_a,
            document_version=1,
            seq=1,
            content="a",
            vector_similarity=0.9,
            keyword_similarity=0.9,
        )
        b = RetrievalChunk(
            chunk_id=uuid.uuid4(),
            document_id=doc_b,
            document_version=1,
            seq=0,
            content="b",
            vector_similarity=0.9,
            keyword_similarity=0.9,
        )
        fused = rrf_fuse([a, b], [b, a])
        assert [x.chunk_id for x in fused] == [a.chunk_id, b.chunk_id]


class TestApplyRerank:
    def test_valid_scores_ordered_desc_keeping_ties_in_rrf_order(self) -> None:
        a = _chunk(seq=0, vector_similarity=0.9)
        b = _chunk(seq=1, vector_similarity=0.8)
        c = _chunk(seq=2, vector_similarity=0.7)
        candidates = [a, b, c]
        # 重排后顺序 0/1/2 → 2/0/1（score 降序）；0 与 1 同分保持 RRF 原顺序。
        scores = [
            RerankScore(candidate_index=0, score=0.6),
            RerankScore(candidate_index=1, score=0.6),
            RerankScore(candidate_index=2, score=0.9),
        ]
        reordered = apply_rerank(candidates, scores)
        assert [c.seq for c in reordered] == [2, 0, 1]
        # 重排后的分数成为引用分数。
        assert reordered[0].fused_score == 0.9

    def test_no_scores_keeps_rrf_order(self) -> None:
        a = _chunk(seq=0, vector_similarity=0.9)
        b = _chunk(seq=1, vector_similarity=0.8)
        candidates = [a, b]
        assert apply_rerank(candidates, None) == [a, b]

    def test_duplicate_or_out_of_range_index_falls_back_to_rrf_order(self) -> None:
        a = _chunk(seq=0, vector_similarity=0.9)
        b = _chunk(seq=1, vector_similarity=0.8)
        for scores in (
            [RerankScore(candidate_index=0, score=1.0), RerankScore(candidate_index=0, score=0.5)],
            [RerankScore(candidate_index=0, score=1.0), RerankScore(candidate_index=7, score=0.5)],
            [RerankScore(candidate_index=0, score=float("nan"))],
            [RerankScore(candidate_index=0, score=float("inf"))],
        ):
            assert apply_rerank([a, b], scores) == [a, b]

    def test_missing_index_falls_back_to_rrf_order(self) -> None:
        a = _chunk(seq=0, vector_similarity=0.9)
        b = _chunk(seq=1, vector_similarity=0.8)
        # 缺项（0 未出现）整体回退。
        assert apply_rerank([a, b], [RerankScore(candidate_index=1, score=1.0)]) == [a, b]


class TestContextPacking:
    def test_token_budget_truncates_tail(self) -> None:
        # 不同资料各 1 段，每段约 900 token；3 段 2700 + 分隔符在预算内，第 4 段被丢弃。
        chunks = [
            RetrievalChunk(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_version=1,
                seq=0,
                content=f"[{i}]" + "w" * 3600,
                vector_similarity=0.9,
            )
            for i in range(4)
        ]
        packed = pack_context(chunks, max_tokens=3000)
        assert estimate_tokens(packed) <= 3000
        assert "[0]" in packed and "[1]" in packed and "[2]" in packed  # 前三段保留
        assert "[3]" not in packed  # 超出预算的尾段被丢弃

    def test_adjacent_same_document_chunks_are_deduplicated(self) -> None:
        same_doc = uuid.uuid4()
        a = RetrievalChunk(
            chunk_id=uuid.uuid4(),
            document_id=same_doc,
            document_version=1,
            seq=0,
            content="first",
            vector_similarity=0.9,
        )
        b = RetrievalChunk(
            chunk_id=uuid.uuid4(),
            document_id=same_doc,
            document_version=1,
            seq=1,
            content="second",
            vector_similarity=0.9,
        )
        c = RetrievalChunk(
            chunk_id=uuid.uuid4(),
            document_id=same_doc,
            document_version=1,
            seq=5,
            content="far",
            vector_similarity=0.9,
        )
        packed = pack_context([a, b, c], max_tokens=3000)
        assert "first" in packed
        assert "second" not in packed  # seq 相邻（0、1）去重
        assert "far" in packed  # seq 不相邻（1、5）保留

    def test_adjacent_dedup_tracks_per_document(self) -> None:
        d1, d2 = uuid.uuid4(), uuid.uuid4()
        chunks = [
            RetrievalChunk(
                chunk_id=uuid.uuid4(),
                document_id=d1,
                document_version=1,
                seq=0,
                content="d1-0",
                vector_similarity=0.9,
            ),
            RetrievalChunk(
                chunk_id=uuid.uuid4(),
                document_id=d2,
                document_version=1,
                seq=0,
                content="d2-0",
                vector_similarity=0.9,
            ),
            RetrievalChunk(
                chunk_id=uuid.uuid4(),
                document_id=d1,
                document_version=1,
                seq=1,
                content="d1-1",
                vector_similarity=0.9,
            ),
        ]
        packed = pack_context(chunks, max_tokens=3000)
        assert "d1-0" in packed
        assert "d2-0" in packed
        assert "d1-1" not in packed

    def test_order_preserved_and_empty_input(self) -> None:
        assert pack_context([], max_tokens=3000) == ""
        chunks = [
            _chunk(seq=0, content="a"),
            RetrievalChunk(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_version=1,
                seq=0,
                content="b",
                vector_similarity=0.9,
            ),
        ]
        assert pack_context(chunks, max_tokens=3000) == "a\n\nb"

    def test_cjk_token_estimation_counts_characters(self) -> None:
        assert estimate_tokens("一二三四") == 4
        assert estimate_tokens("hello world") == 3  # ceil(10/4)
