"""检索融合、重排与上下文打包服务（T069 / FR-015、FR-017、plan.md 决策 4）。

- 双路召回只消费通过证据门槛的候选（SQL 门槛由 ChunkRepository 保证）；
- RRF 融合：跨路命中候选加权；排序确定性（融合分降序，同分按
  ``(document_id, seq)``）；
- 可选 Reranker：合法评分按 score 降序且同分保持 RRF 原顺序；缺失/重复/越界/
  非有限评分或网关失败整体回退原 RRF 顺序；
- Context Pack：3000 token 预算 + 相邻片段去重（同资料相邻 seq 只保留一个）；
- 融合为空时返回空候选，由问答服务收敛为可信无证据答复，不调用生成模型。
"""

import math
import re
import uuid
from dataclasses import dataclass, replace
from typing import Protocol

from app.infrastructure.model_gateway.types import RerankScore
from app.repositories.chunks import ChunkRepository, RetrievalChunk
from app.services.llm.embeddings import EmbeddingFailure, EmbeddingService
from app.services.llm.reranker import validate_rerank_scores
from app.services.retrieval_config import RetrievalSettings

# 上下文打包默认 token 预算（代码常量；quickstart 检索契约）。
CONTEXT_MAX_TOKENS = 3000

# CJK 统一表意文字等按单字符计 token（与拉丁词近似粒度对齐）。
_CJK_RE = re.compile(r"[⺀-鿿豈-﫿＀-￯]")

# RRF 常数 k（秩融合平滑项）。
_RRF_K = 60


@dataclass(frozen=True)
class RetrievalResult:
    """检索输出：融合后候选（已按最终顺序）与上下文包。"""

    query: str
    candidates: tuple[RetrievalChunk, ...]
    context_pack: str


class RerankerPort(Protocol):
    """可选重排端口；未配置或失败时返回 None（回退 RRF）。"""

    def rerank_scores(
        self, *, user_id: uuid.UUID, query: str, candidates: list[RetrievalChunk]
    ) -> list[RerankScore] | None: ...


def estimate_tokens(text: str) -> int:
    """CJK 字符逐个计 token；其余按 4 字符/token 近似。"""
    cjk = len(_CJK_RE.findall(text))
    rest = len(_CJK_RE.sub("", text))
    return cjk + math.ceil(rest / 4)


def rrf_fuse(
    vector: list[RetrievalChunk], keyword: list[RetrievalChunk], *, k: int = _RRF_K
) -> list[RetrievalChunk]:
    """RRF 融合两路候选；只消费已通过门槛的输入，返回按融合分降序的列表。"""
    entries: dict[uuid.UUID, dict] = {}
    for source, candidates in (("vector", vector), ("keyword", keyword)):
        for rank, candidate in enumerate(candidates, start=1):
            entry = entries.setdefault(
                candidate.chunk_id,
                {"candidate": candidate, "fused": 0.0, "ranks": {}},
            )
            entry["ranks"][source] = rank
            entry["fused"] += 1.0 / (k + rank)
    ordered = sorted(
        entries.values(),
        key=lambda e: (
            -e["fused"],
            e["candidate"].document_id,
            e["candidate"].seq,
        ),
    )
    return [
        replace(
            entry["candidate"],
            fused_score=round(entry["fused"], 6),
            vector_similarity=(
                entry["candidate"].vector_similarity if "vector" in entry["ranks"] else None
            ),
            keyword_similarity=(
                entry["candidate"].keyword_similarity if "keyword" in entry["ranks"] else None
            ),
        )
        for entry in ordered
    ]


def apply_rerank(
    candidates: list[RetrievalChunk], scores: list[RerankScore] | None
) -> list[RetrievalChunk]:
    """应用合法重排评分；None 或评分不完整整体回退原 RRF 顺序。

    合法评分按 score 降序排列，同分保持候选原有（RRF）顺序；重排后的
    score 成为引用分数。评分完整性校验复用 Reranker 适配器的同一函数。
    """
    if scores is None or not validate_rerank_scores(scores, len(candidates)):
        return list(candidates)
    by_index = {score.candidate_index: score.score for score in scores}
    ordered = sorted(range(len(candidates)), key=lambda i: (-by_index[i], i))
    return [replace(candidates[i], fused_score=by_index[i]) for i in ordered]


def pack_context(candidates: list[RetrievalChunk], *, max_tokens: int = CONTEXT_MAX_TOKENS) -> str:
    """按最终顺序打包上下文；同资料相邻 seq 片段去重，超出预算的尾段丢弃。"""
    parts: list[str] = []
    budget = max_tokens
    last_kept_seq: dict[uuid.UUID, int] = {}
    for candidate in candidates:
        if (
            candidate.document_id in last_kept_seq
            and candidate.seq == last_kept_seq[candidate.document_id] + 1
        ):
            continue  # 相邻片段去重（同资料连续 seq 只保留第一个）
        tokens = estimate_tokens(candidate.content)
        if tokens > budget:
            continue
        parts.append(candidate.content)
        budget -= tokens
        last_kept_seq[candidate.document_id] = candidate.seq
    return "\n\n".join(parts)


class RetrievalService:
    """双路召回 + RRF + 可选重排 + 上下文打包的检索用例。"""

    def __init__(
        self,
        session,
        *,
        embedding_service: EmbeddingService | None = None,
        reranker: RerankerPort | None = None,
        settings: RetrievalSettings | None = None,
    ) -> None:
        self.chunk_repository = ChunkRepository(session)
        self.embedding_service = embedding_service or EmbeddingService()
        self.reranker = reranker
        self.settings = settings or _default_retrieval_settings()

    def count_retrievable(self, user_id: uuid.UUID, knowledge_base_id: uuid.UUID) -> int:
        """当前可检索片段数（无完成资料时问答服务返回 20005/409）。"""
        return self.chunk_repository.count_retrievable(user_id, knowledge_base_id)

    def retrieve(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        query: str,
        *,
        trace_id: str | None = None,
    ) -> RetrievalResult:
        """检索当前用户知识库：向量 + 关键词双路召回 → RRF → 可选重排 → 上下文包。"""
        vector_hits: list[RetrievalChunk] = []
        try:
            query_vector = self.embedding_service.embed_texts(
                [query], user_id=user_id, trace_id=trace_id
            )[0]
            vector_hits = self.chunk_repository.vector_search(
                user_id,
                knowledge_base_id,
                query_vector,
                self.settings.vector_min_similarity,
            )
        except EmbeddingFailure:
            # 查询侧嵌入失败：退化为仅关键词召回（关键词路径仍是有效证据）。
            vector_hits = []
        keyword_hits = self.chunk_repository.keyword_search(
            user_id, knowledge_base_id, query, self.settings.trgm_min_similarity
        )
        fused = rrf_fuse(vector_hits, keyword_hits)
        if fused and self.reranker is not None:
            scores = self.reranker.rerank_scores(user_id=user_id, query=query, candidates=fused)
            fused = apply_rerank(fused, scores)
        context_pack = pack_context(fused)
        return RetrievalResult(query=query, candidates=tuple(fused), context_pack=context_pack)


def _default_retrieval_settings() -> RetrievalSettings:
    from app.core.settings import get_settings

    return get_settings().retrieval
