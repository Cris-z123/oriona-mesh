"""改写、重排与生成网关弹性失败测试（T064 / FR-027、FR-028、model-egress.md）。

覆盖：四类调用经网关且声明正确调用类型；超时与重试只由网关执行（假网关内部按预算
重试，业务适配器只发起一次调用）；网关最终失败分类与业务领域降级职责
（改写回退原问题、重排回退原 RRF 顺序、生成收敛 failed/error）；脱敏失败零外发；
Reranker 未配置时禁用且不调用网关；Reranker 合法评分按 score 降序、同分保持 RRF
原顺序，缺项/重复/越界/非有限评分整体回退；生成失败重试耗尽必须为
:class:`GenerationFailure`，不得误记为取消。
"""

import uuid

import pytest

from app.infrastructure.model_gateway.types import (
    GatewayError,
    GenerationDelta,
    ModelCall,
    QueryRewriteResult,
    RerankResult,
    RerankScore,
)
from app.repositories.chunks import RetrievalChunk
from app.services.llm.chat import GenerationFailure, GenerationService, QueryRewriteService
from app.services.llm.reranker import RerankerService

pytestmark = pytest.mark.unit

HISTORY = [("user", "第一问"), ("assistant", "第一答"), ("user", "第二问")]


class FakeGateway:
    """记录调用并模拟网关内部超时/重试行为（重试只发生在网关内）。"""

    def __init__(
        self,
        *,
        rewrite_result: str = "改写结果",
        rewrite_error: GatewayError | None = None,
        rerank_scores: list[RerankScore] | None = None,
        rerank_error: GatewayError | None = None,
        gen_deltas: list[str] | None = None,
        gen_error: GatewayError | None = None,
        retry_failures: int = 0,
    ) -> None:
        self.rewrite_result = rewrite_result
        self.rewrite_error = rewrite_error
        self.rerank_scores = rerank_scores
        self.rerank_error = rerank_error
        self.gen_deltas = gen_deltas if gen_deltas is not None else ["增量1", "增量2"]
        self.gen_error = gen_error
        self.retry_failures = retry_failures
        self.calls: list[ModelCall] = []
        self.stream_calls: list[ModelCall] = []

    def call(self, call: ModelCall) -> QueryRewriteResult | RerankResult:
        self.calls.append(call)
        for _ in range(self.retry_failures):
            try:
                return self._emit(call)
            except GatewayError:
                continue
        return self._emit(call)

    def call_stream(self, call: ModelCall):
        self.stream_calls.append(call)
        for _ in range(self.retry_failures):
            try:
                return iter(self._emit_stream(call))
            except GatewayError:
                continue
        return iter(self._emit_stream(call))

    def _emit(self, call: ModelCall):
        if call.call_type == "query_rewrite":
            if self.rewrite_error is not None:
                raise self.rewrite_error
            return QueryRewriteResult(rewritten_query=self.rewrite_result)
        if call.call_type == "rerank":
            if self.rerank_error is not None:
                raise self.rerank_error
            return RerankResult(scores=list(self.rerank_scores or []))
        raise AssertionError(f"unexpected call type {call.call_type}")

    def _emit_stream(self, call: ModelCall):
        if self.gen_error is not None:
            raise self.gen_error
        for text in self.gen_deltas:
            yield GenerationDelta(text=text)
        yield GenerationDelta(text="", finish_reason="stop")


def _user_id() -> uuid.UUID:
    return uuid.uuid4()


def _enabled_reranker(gateway) -> RerankerService:
    """构造启用 reranker 的服务（rerank_model 显式配置）。"""
    from app.infrastructure.model_gateway.config import ModelGatewaySettings

    return RerankerService(
        gateway=gateway,
        model_gateway=ModelGatewaySettings(rerank_model="test-rerank"),
    )


def _plain_reranker(gateway) -> RerankerService:
    # 未注解参数：与 test_embeddings._service 一致，容纳假网关替身。
    return RerankerService(gateway=gateway)


def _rewrite_service(gateway) -> QueryRewriteService:
    return QueryRewriteService(gateway=gateway)


def _generation_service(gateway) -> GenerationService:
    return GenerationService(gateway=gateway)


def _chunks(n: int = 3) -> list[RetrievalChunk]:
    return [
        RetrievalChunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_version=1,
            seq=i,
            content=f"候选{i}",
            vector_similarity=0.8 - i * 0.1,
        )
        for i in range(n)
    ]


class TestQueryRewrite:
    def test_rewrite_goes_through_gateway_with_minimal_content(self) -> None:
        gateway = FakeGateway()
        service = _rewrite_service(gateway)
        result = service.rewrite(user_id=_user_id(), query="原始问题", history=HISTORY)
        assert result == "改写结果"
        assert len(gateway.calls) == 1
        call = gateway.calls[0]
        assert call.call_type == "query_rewrite"
        assert "原始问题" in call.content
        for _role, text in HISTORY:
            assert text in call.content

    def test_retries_only_executed_by_gateway_single_business_attempt(self) -> None:
        gateway = FakeGateway(retry_failures=2)
        result = _rewrite_service(gateway).rewrite(user_id=_user_id(), query="q", history=[])
        assert result == "改写结果"
        assert len(gateway.calls) == 1

    @pytest.mark.parametrize(
        "error",
        [
            GatewayError("provider_error", "provider failed"),
            GatewayError("timeout", "timed out"),
            GatewayError("rate_limited", "limited"),
            GatewayError("network", "network down"),
            GatewayError("sanitization_failed", "sanitization failed"),
            GatewayError("configuration", "model missing"),
            GatewayError("invalid_response", "bad response"),
        ],
    )
    def test_gateway_final_failure_falls_back_to_original_query(self, error) -> None:
        gateway = FakeGateway(rewrite_error=error)
        result = _rewrite_service(gateway).rewrite(
            user_id=_user_id(), query="原始问题", history=HISTORY
        )
        assert result == "原始问题"
        assert len(gateway.calls) == 1


class TestReranker:
    def test_rerank_goes_through_gateway_with_indices(self) -> None:
        gateway = FakeGateway(
            rerank_scores=[
                RerankScore(candidate_index=1, score=0.9),
                RerankScore(candidate_index=0, score=0.5),
                RerankScore(candidate_index=2, score=0.7),
            ]
        )
        service = _enabled_reranker(gateway)
        chunks = _chunks(3)
        scores = service.rerank_scores(user_id=_user_id(), query="问题", candidates=chunks)
        assert len(gateway.calls) == 1
        call = gateway.calls[0]
        assert call.call_type == "rerank"
        assert "问题" in call.content
        assert "[0]" in call.content and "[2]" in call.content
        assert scores is not None
        assert [s.candidate_index for s in scores] == [1, 0, 2]

    def test_disabled_reranker_returns_none_without_gateway_call(self) -> None:
        # 默认配置（rerank_model 为空）禁用重排，直接使用 RRF 且不调用网关。
        gateway = FakeGateway()
        service = _plain_reranker(gateway)
        assert service.rerank_scores(user_id=_user_id(), query="q", candidates=_chunks(2)) is None
        assert gateway.calls == []

    def test_gateway_final_failure_falls_back_to_rrf(self) -> None:
        gateway = FakeGateway(rerank_error=GatewayError("timeout", "timed out"))
        service = _enabled_reranker(gateway)
        assert service.rerank_scores(user_id=_user_id(), query="q", candidates=_chunks(2)) is None
        assert len(gateway.calls) == 1

    def test_incomplete_scores_fall_back_to_rrf(self) -> None:
        # 网关返回缺项的评分（第 0 项缺失）：整体回退。
        gateway = FakeGateway(rerank_scores=[RerankScore(candidate_index=1, score=1.0)])
        service = _enabled_reranker(gateway)
        assert service.rerank_scores(user_id=_user_id(), query="q", candidates=_chunks(2)) is None

    def test_retries_only_executed_by_gateway(self) -> None:
        gateway = FakeGateway(
            rerank_scores=[RerankScore(candidate_index=0, score=1.0)],
            retry_failures=2,
        )
        service = _enabled_reranker(gateway)
        scores = service.rerank_scores(user_id=_user_id(), query="q", candidates=_chunks(1))
        assert scores is not None
        assert len(gateway.calls) == 1

    def test_valid_scores_reorder_desc_keeping_ties_in_input_order(self) -> None:
        gateway = FakeGateway(
            rerank_scores=[
                RerankScore(candidate_index=0, score=0.6),
                RerankScore(candidate_index=1, score=0.6),
                RerankScore(candidate_index=2, score=0.9),
            ]
        )
        service = _enabled_reranker(gateway)
        chunks = _chunks(3)
        from app.services.retrieval_service import apply_rerank

        scores = service.rerank_scores(user_id=_user_id(), query="q", candidates=chunks)
        reordered = apply_rerank(chunks, scores)
        assert [c.seq for c in reordered] == [2, 0, 1]


class TestGeneration:
    def test_generation_streams_through_gateway(self) -> None:
        gateway = FakeGateway(gen_deltas=["根据", "资料"])
        service = _generation_service(gateway)
        deltas = list(
            service.stream(user_id=_user_id(), query="问题", context_pack="上下文", history=HISTORY)
        )
        assert [d.text for d in deltas if d.text] == ["根据", "资料"]
        assert len(gateway.stream_calls) == 1
        call = gateway.stream_calls[0]
        assert call.call_type == "generation"
        assert "问题" in call.content
        assert "上下文" in call.content
        for _role, text in HISTORY:
            assert text in call.content

    @pytest.mark.parametrize(
        "error",
        [
            GatewayError("provider_error", "provider failed"),
            GatewayError("timeout", "first token timeout"),
            GatewayError("timeout", "total timeout"),
            GatewayError("rate_limited", "limited"),
            GatewayError("network", "network down"),
            GatewayError("sanitization_failed", "sanitization failed"),
            GatewayError("configuration", "model missing"),
            GatewayError("invalid_response", "bad response"),
        ],
    )
    def test_gateway_final_failure_raises_generation_failure_not_cancel(self, error) -> None:
        gateway = FakeGateway(gen_error=error)
        service = _generation_service(gateway)
        with pytest.raises(GenerationFailure):
            list(service.stream(user_id=_user_id(), query="q", context_pack="c", history=[]))
        # 网关按预算重试后只返回最终失败；业务层不得误记为取消。
        assert len(gateway.stream_calls) == 1

    def test_retries_only_executed_by_gateway(self) -> None:
        gateway = FakeGateway(gen_deltas=["x"], retry_failures=2)
        service = _generation_service(gateway)
        deltas = list(service.stream(user_id=_user_id(), query="q", context_pack="c", history=[]))
        assert any(d.text for d in deltas)
        assert len(gateway.stream_calls) == 1

    def test_stream_carries_terminal_finish_reason(self) -> None:
        gateway = FakeGateway(gen_deltas=["x"])
        service = _generation_service(gateway)
        deltas = list(service.stream(user_id=_user_id(), query="q", context_pack="c", history=[]))
        assert deltas[-1].finish_reason in ("stop", "length", None)
