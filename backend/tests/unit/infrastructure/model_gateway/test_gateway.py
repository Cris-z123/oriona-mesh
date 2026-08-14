"""模型出口网关单元测试（T036 / FR-027、FR-028）。

覆盖：四类调用路由与单次物理请求、脱敏失败零外发、重试只由网关按配置预算执行且无
业务层/LangChain 隐式请求、网关最终失败分类且不执行领域降级、Reranker 完整评分与
非法响应整体回退、endpoint/provider 配置规则与日志白名单。
"""

import uuid
from typing import Any

import pytest
from openai import APITimeoutError
from pydantic import SecretStr
from pydantic_core import ValidationError

from app.infrastructure.model_gateway.audit import ALLOWED_AUDIT_FIELDS, ModelCallAudit
from app.infrastructure.model_gateway.config import ModelGatewaySettings
from app.infrastructure.model_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from app.infrastructure.model_gateway.service import ModelGatewayService
from app.infrastructure.model_gateway.types import (
    EmbeddingResult,
    GatewayError,
    GenerationResult,
    ModelCall,
    QueryRewriteResult,
    RerankResult,
    RerankScore,
    SanitizedModelCall,
)


def _settings(**overrides: Any) -> ModelGatewaySettings:
    base: dict[str, Any] = {
        "endpoint": "https://api.example.com/v1",
        "api_key": SecretStr("sk-test"),
        "query_rewrite_model": "rewrite-model",
        "generation_model": "gen-model",
    }
    base.update(overrides)
    return ModelGatewaySettings(**base)


def _call(call_type: str, content: str = "plain text", **options: Any) -> ModelCall:
    return ModelCall(
        call_id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        subject_digest="d" * 64,
        call_type=call_type,  # type: ignore[arg-type]
        content=content,
        options=options,
    )


class _FakeAdapter:
    """记录调用次数的假供应商适配器（断言单次物理请求与重试预算）。"""

    name = "fake-provider"

    def __init__(self) -> None:
        self.calls: list[tuple[str, SanitizedModelCall]] = []
        self.fail_first: int = 0
        self.fail_with: Exception | None = None
        # 流式控制：首块前抛错（仅前 stream_raise_times 次）/ 首块后停流
        self.stream_raise_before_first: Exception | None = None
        self.stream_raise_times: int = 0
        self.stream_stall_after_first: bool = False

    def _record(self, kind: str, call: SanitizedModelCall):
        self.calls.append((kind, call))
        if len(self.calls) <= self.fail_first and self.fail_with is not None:
            raise self.fail_with

    def embed(self, call: SanitizedModelCall) -> EmbeddingResult:
        self._record("embed", call)
        return EmbeddingResult(vectors=[[0.1, 0.2]])

    def chat(self, call: SanitizedModelCall) -> QueryRewriteResult | GenerationResult:
        self._record("chat", call)
        if call.call_type == "generation":
            return GenerationResult(content="answer", finish_reason="stop")
        return QueryRewriteResult(rewritten_query="rewritten")

    def chat_stream(self, call: SanitizedModelCall):
        self._record("stream", call)

        def gen():
            for i, chunk in enumerate(["a", "b"]):
                if (
                    i == 0
                    and self.stream_raise_before_first is not None
                    and self.stream_raise_times > 0
                ):
                    self.stream_raise_times -= 1
                    raise self.stream_raise_before_first
                if i >= 1 and self.stream_stall_after_first:
                    import time as _t

                    _t.sleep(3600)
                yield chunk

        return gen()

    def rerank(self, call: SanitizedModelCall) -> RerankResult:
        self._record("rerank", call)
        return RerankResult(scores=[RerankScore(candidate_index=0, score=0.9)])

    @staticmethod
    def classify_exception(exc: Exception) -> GatewayError:
        if isinstance(exc, APITimeoutError):
            return GatewayError("timeout", "provider request timeout")
        if isinstance(exc, ConnectionError):
            return GatewayError("network", "provider connection failed")
        return GatewayError("provider_error", "provider call failed")


class TestCallRouting:
    @pytest.mark.parametrize(
        ("call_type", "method"),
        [
            ("embedding", "embed"),
            ("query_rewrite", "chat"),
            ("generation", "chat"),
            ("rerank", "rerank"),
        ],
    )
    def test_routes_to_adapter_single_physical_request(self, call_type: str, method: str) -> None:
        adapter = _FakeAdapter()
        overrides: dict[str, Any] = (
            {"rerank_model": "rerank-model"} if call_type == "rerank" else {}
        )
        service = ModelGatewayService(_settings(**overrides), adapter=adapter)
        result = service.call(_call(call_type))
        assert [kind for kind, _ in adapter.calls] == [method]
        if call_type == "rerank":
            assert result.scores[0].candidate_index == 0  # type: ignore[union-attr]

    def test_sanitized_call_has_passed_status_and_config_timeout(self) -> None:
        adapter = _FakeAdapter()
        settings = _settings(embedding_timeout_seconds=30, embedding_max_retries=2)
        service = ModelGatewayService(settings, adapter=adapter)
        service.call(_call("embedding"))
        _, sanitized = adapter.calls[0]
        assert sanitized.sanitization_status == "passed"
        assert sanitized.timeout_seconds == 30
        assert sanitized.max_retries == 2
        assert sanitized.provider == "fake-provider"

    def test_model_comes_from_config_not_caller(self) -> None:
        adapter = _FakeAdapter()
        service = ModelGatewayService(_settings(), adapter=adapter)
        service.call(_call("embedding", options={"model": "hijack-model"}))
        _, sanitized = adapter.calls[0]
        assert sanitized.model == "text-embedding-3-small"
        assert sanitized.model != "hijack-model"


class TestSanitizeFailClosed:
    def test_sanitization_failure_produces_zero_external_requests(self) -> None:
        adapter = _FakeAdapter()
        service = ModelGatewayService(_settings(), adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            service.call(_call("generation", content="leak token=abc123def"))
        assert exc.value.error_class == "sanitization_failed"
        assert adapter.calls == []


class TestRetries:
    def test_retries_executed_only_by_gateway_budget(self) -> None:
        adapter = _FakeAdapter()
        adapter.fail_with = APITimeoutError(request=None)  # type: ignore[arg-type]
        adapter.fail_first = 2
        settings = _settings(embedding_max_retries=2)
        service = ModelGatewayService(settings, adapter=adapter)
        result = service.call(_call("embedding"))
        assert isinstance(result, EmbeddingResult)
        assert result.vectors == [[0.1, 0.2]]
        # 初次 + 2 次重试 = 3 次物理请求，无额外隐式重试。
        assert len(adapter.calls) == 3

    def test_retry_exhausted_final_failure(self) -> None:
        adapter = _FakeAdapter()
        adapter.fail_with = APITimeoutError(request=None)  # type: ignore[arg-type]
        adapter.fail_first = 99
        settings = _settings(embedding_max_retries=2)
        service = ModelGatewayService(settings, adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            service.call(_call("embedding"))
        assert exc.value.error_class == "timeout"
        assert len(adapter.calls) == 3  # 初次 + 2 次重试后收敛，无领域降级

    def test_no_domain_degradation_in_gateway(self) -> None:
        # 网关不执行领域降级：最终失败直接抛出，不返回回退内容。
        adapter = _FakeAdapter()
        adapter.fail_with = APITimeoutError(request=None)  # type: ignore[arg-type]
        adapter.fail_first = 99
        service = ModelGatewayService(_settings(), adapter=adapter)
        with pytest.raises(GatewayError):
            service.call(_call("query_rewrite"))
        assert len(adapter.calls) == 2  # query_rewrite 默认 1 次重试

    def test_missing_model_is_configuration_error_without_request(self) -> None:
        adapter = _FakeAdapter()
        settings = _settings(query_rewrite_model=None)
        service = ModelGatewayService(settings, adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            service.call(_call("query_rewrite"))
        assert exc.value.error_class == "configuration"
        assert adapter.calls == []


class TestRerankStrictness:
    def test_complete_scores_passed_through(self) -> None:
        raw = '{"scores":[{"candidate_index":1,"score":0.7},{"candidate_index":0,"score":0.9}]}'
        scores = OpenAICompatibleAdapter._parse_rerank_scores(raw, 2)
        assert [s.candidate_index for s in scores] == [1, 0]

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not json",
            '{"scores":[]}',
            '{"extra":1,"scores":[{"candidate_index":0,"score":0.5}]}',
            '[{"candidate_index":0,"score":0.5}]',
            '{"scores":[{"candidate_index":0}]}',
            '{"scores":[{"candidate_index":0,"score":"high"}]}',
            '{"scores":[{"candidate_index":0,"score":0.5},{"candidate_index":0,"score":0.4}]}',
            '{"scores":[{"candidate_index":5,"score":0.5}]}',
            '{"scores":[{"candidate_index":0,"score":0.5},{"candidate_index":0,"score":0.4}]}',
        ],
    )
    def test_invalid_response_fails_whole_rerank(self, raw: str) -> None:
        with pytest.raises(GatewayError) as exc:
            OpenAICompatibleAdapter._parse_rerank_scores(raw, 2)
        assert exc.value.error_class == "invalid_response"

    def test_non_finite_score_rejected(self) -> None:
        with pytest.raises(GatewayError):
            OpenAICompatibleAdapter._parse_rerank_scores(
                '{"scores":[{"candidate_index":0,"score":NaN},{"candidate_index":1,"score":0.5}]}',
                2,
            )


class TestEndpointAndProviderRules:
    def test_https_endpoint_accepted(self) -> None:
        assert (
            _settings(endpoint="https://api.example.com/v1").endpoint
            == "https://api.example.com/v1"
        )

    @pytest.mark.parametrize(
        "endpoint", ["http://localhost:8000/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"]
    )
    def test_loopback_http_accepted(self, endpoint: str) -> None:
        assert _settings(endpoint=endpoint).endpoint == endpoint

    def test_other_http_endpoint_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(endpoint="http://api.example.com/v1")

    def test_invalid_endpoint_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(endpoint="not-a-url")

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(provider="anthropic")

    def test_audit_payloads_must_stay_false(self) -> None:
        with pytest.raises(ValidationError):
            _settings(audit_payloads=True)


class TestAuditWhitelist:
    def test_audit_fields_are_whitelisted_only(self) -> None:
        audit = ModelCallAudit(
            trace_id="t",
            call_id="c",
            subject_digest="d",
            call_type="generation",
            provider="fake",
            model="m",
            status="failed",
            started_at=0.0,
            error_class="timeout",
            retries=2,
        )
        event = audit.to_whitelisted()
        assert set(event) <= set(ALLOWED_AUDIT_FIELDS)
        # 不允许任何正文/提示词字段混入。
        for forbidden in ("content", "prompt", "payload", "headers", "message"):
            assert forbidden not in event


class TestCallStream:
    """流式生成路径（C1 修复后的行为契约）。"""

    def _stream(self, service, call):
        return list(service.call_stream(call))

    def test_stream_yields_deltas_and_terminal_delta(self) -> None:
        adapter = _FakeAdapter()
        service = ModelGatewayService(_settings(), adapter=adapter)
        deltas = self._stream(service, _call("generation"))
        assert [d.text for d in deltas if d.text] == ["a", "b"]
        assert deltas[-1].finish_reason == "stop"
        assert [kind for kind, _ in adapter.calls] == ["stream"]

    def test_stream_retries_on_first_chunk_failure(self) -> None:
        # 首次流调用在首块前抛错（生成器体内）：网关按预算重试，物理请求 1+1 次。
        adapter = _FakeAdapter()
        adapter.stream_raise_before_first = APITimeoutError(request=None)  # type: ignore[arg-type]
        adapter.stream_raise_times = 1
        settings = _settings(generation_max_retries=1)
        service = ModelGatewayService(settings, adapter=adapter)
        deltas = self._stream(service, _call("generation"))
        assert [d.text for d in deltas if d.text] == ["a", "b"]
        assert len(adapter.calls) == 2  # 初次 + 1 次重试

    def test_stream_retry_exhausted_final_failure(self) -> None:
        adapter = _FakeAdapter()
        adapter.stream_raise_before_first = APITimeoutError(request=None)  # type: ignore[arg-type]
        adapter.stream_raise_times = 99  # 每次都失败
        settings = _settings(generation_max_retries=1)
        service = ModelGatewayService(settings, adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            self._stream(service, _call("generation"))
        assert exc.value.error_class == "timeout"
        assert len(adapter.calls) == 2  # 初次 + 1 次重试后收敛

    def test_stream_sanitization_failure_zero_requests(self) -> None:
        adapter = _FakeAdapter()
        service = ModelGatewayService(_settings(), adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            self._stream(service, _call("generation", content="leak token=abc"))
        assert exc.value.error_class == "sanitization_failed"
        assert adapter.calls == []

    def test_stream_first_token_timeout(self) -> None:
        # 首块前停流（生成器体 sleep）：首 token 超时由网关收敛为 GatewayError(timeout)。
        import time as _time

        adapter = _FakeAdapter()

        def slow_gen():
            _time.sleep(5)
            yield "late"

        def chat_stream(call: SanitizedModelCall):
            adapter._record("stream", call)
            return slow_gen()

        adapter.chat_stream = chat_stream  # type: ignore[method-assign]
        settings = _settings(generation_first_token_timeout_seconds=1, generation_max_retries=0)
        service = ModelGatewayService(settings, adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            self._stream(service, _call("generation"))
        assert exc.value.error_class == "timeout"

    def test_stream_total_timeout_on_stalled_stream(self) -> None:
        # 首块后停流：总时长上限由网关执行（不再无限阻塞）。
        adapter = _FakeAdapter()
        adapter.stream_stall_after_first = True
        settings = _settings(generation_total_timeout_seconds=1, generation_max_retries=0)
        service = ModelGatewayService(settings, adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            self._stream(service, _call("generation"))
        assert exc.value.error_class == "timeout"

    def test_stream_mid_stream_adapter_error_classified(self) -> None:
        adapter = _FakeAdapter()

        def failing_gen():
            yield "a"
            raise ConnectionError("dropped")

        def chat_stream(call: SanitizedModelCall):
            adapter._record("stream", call)
            return failing_gen()

        adapter.chat_stream = chat_stream  # type: ignore[method-assign]
        service = ModelGatewayService(_settings(), adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            self._stream(service, _call("generation"))
        assert exc.value.error_class == "network"
