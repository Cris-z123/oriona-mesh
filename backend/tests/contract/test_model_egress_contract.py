"""内部模型出口契约测试（T090 / model-egress.md）。

契约来源：``specs/001-orionamesh-rag-mvp/contracts/model-egress.md``。以"契约→实现"
结构断言方式校验 model_gateway 与业务适配器实现与契约文本一致（纯单元级，无
数据库/Redis/网络）：
1. 四类调用类型枚举（embedding/query_rewrite/rerank/generation）；
2. 调用信封最小字段与必填性；
3. 业务调用方不得指定凭证/endpoint/模型；
4. 脱敏状态 passed 与供应商适配层前置条件（只收 SanitizedModelCall）；
5. endpoint 必填与 openai-compatible provider 规则（HTTP 仅限回环）；
6. 网关是超时/重试的唯一执行者（业务适配器无重试逻辑）；
7. 单次供应商适配请求（重试由网关编排，物理请求 1+max_retries）；
8. 网关最终失败分类与业务领域降级职责分离（不包装第二层重试）；
9. Reranker scores[{candidate_index,score}] 完整性/整体回退；
10. 未知 provider 拒绝（settings 与 factory 双层）；
11. 日志元数据白名单与契约一致；
12. 脱敏规则（禁止字段删除、PII 不可逆占位符、fail-closed 零外发）。
"""

import ast
import dataclasses
import inspect
import math
import uuid
from types import SimpleNamespace
from typing import Any, get_args, get_type_hints

import pytest
from openai import APITimeoutError
from pydantic import SecretStr
from pydantic_core import ValidationError

import app.services.llm.chat as chat_module
import app.services.llm.embeddings as embeddings_module
import app.services.llm.reranker as reranker_module
from app.core.readiness import _validate_model_gateway
from app.core.settings import Settings
from app.infrastructure.model_gateway.audit import (
    ALLOWED_AUDIT_FIELDS,
    ModelCallAudit,
    log_model_call,
)
from app.infrastructure.model_gateway.config import ModelGatewaySettings
from app.infrastructure.model_gateway.providers.base import ProviderAdapter
from app.infrastructure.model_gateway.providers.factory import build_provider_adapter
from app.infrastructure.model_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from app.infrastructure.model_gateway.sanitizer import sanitize_call
from app.infrastructure.model_gateway.service import ModelGatewayService
from app.infrastructure.model_gateway.types import (
    CallType,
    EmbeddingResult,
    GatewayError,
    GenerationResult,
    ModelCall,
    QueryRewriteResult,
    RerankResult,
    RerankScore,
    SanitizedModelCall,
)
from app.infrastructure.rate_limit.config import RateLimitSettings
from app.repositories.chunks import RetrievalChunk
from app.services.llm.chat import GenerationFailure, GenerationService, QueryRewriteService
from app.services.llm.embeddings import EmbeddingFailure, EmbeddingService
from app.services.llm.reranker import RerankerService

pytestmark = pytest.mark.contract

# model-egress.md 日志元数据白名单：只允许这些键（第 11 条断言全集相等）。
EXPECTED_AUDIT_FIELDS = {
    "trace_id",
    "call_id",
    "subject_digest",
    "call_type",
    "provider",
    "model",
    "started_at",
    "finished_at",
    "duration_ms",
    "status",
    "error_class",
    "retries",
    "input_tokens",
    "output_tokens",
    "payload_bytes",
}


def _field_names(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _gateway_settings(**overrides: Any) -> ModelGatewaySettings:
    base: dict[str, Any] = {
        "endpoint": "https://api.example.com/v1",
        "api_key": SecretStr("sk-test"),
        "query_rewrite_model": "rewrite-model",
        "generation_model": "gen-model",
    }
    base.update(overrides)
    return ModelGatewaySettings(**base)


def _app_settings() -> Settings:
    """最小合法应用配置（业务适配器构造注入，不依赖环境变量文件）。"""
    return Settings(
        auth_jwt_secret_key=SecretStr("j" * 40),
        rate_limit=RateLimitSettings(subject_hmac_key=SecretStr("r" * 40)),
        model_gateway=_gateway_settings(),
    )


def _call(call_type: str, content: str = "plain text", **options: Any) -> ModelCall:
    return ModelCall(
        call_id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        subject_digest="d" * 64,
        call_type=call_type,  # type: ignore[arg-type]
        content=content,
        options=options,
    )


def _sanitized_call(**overrides: Any) -> SanitizedModelCall:
    base: dict[str, Any] = {
        "call_id": "c-1",
        "trace_id": "t-1",
        "subject_digest": "d" * 64,
        "call_type": "embedding",
        "sanitization_status": "passed",
        "policy_version": "v1",
        "provider": "fake-provider",
        "model": "text-embedding-3-small",
        "sanitized_content": "plain text",
        "options": {},
        "timeout_seconds": 30.0,
        "max_retries": 2,
    }
    base.update(overrides)
    return SanitizedModelCall(**base)


class _CountingAdapter:
    """记录适配器方法调用次数的假供应商（物理请求边界）；可按需抛错。"""

    name = "counting"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.embed_calls = 0
        self.chat_calls = 0
        self.rerank_calls = 0

    def _record(self) -> None:
        if self.error is not None:
            raise self.error

    def embed(self, call: SanitizedModelCall) -> EmbeddingResult:
        self.embed_calls += 1
        self._record()
        return EmbeddingResult(vectors=[[0.1, 0.2]])

    def chat(self, call: SanitizedModelCall) -> QueryRewriteResult | GenerationResult:
        self.chat_calls += 1
        self._record()
        if call.call_type == "generation":
            return GenerationResult(content="ok", finish_reason="stop")
        return QueryRewriteResult(rewritten_query="ok")

    def chat_stream(self, call: SanitizedModelCall):
        self.chat_calls += 1
        self._record()
        yield "ok"

    def rerank(self, call: SanitizedModelCall) -> RerankResult:
        self.rerank_calls += 1
        self._record()
        return RerankResult(scores=[])

    @staticmethod
    def classify_exception(exc: Exception) -> GatewayError:
        if isinstance(exc, APITimeoutError):
            return GatewayError("timeout", "provider request timeout")
        return GatewayError("provider_error", "provider call failed")


class _PreconditionAdapter(_CountingAdapter):
    """模拟真实适配器的前置条件检查：未标记已脱敏的调用对象不得发起物理请求。"""

    def embed(self, call: SanitizedModelCall) -> EmbeddingResult:
        OpenAICompatibleAdapter._require_passed(call)
        return super().embed(call)


class _FailingGateway:
    """网关最终失败替身；统计业务适配器的调用次数（不得有第二层重试）。"""

    def __init__(self, error: GatewayError) -> None:
        self.error = error
        self.call_count = 0
        self.stream_call_count = 0

    def call(self, call: ModelCall):
        self.call_count += 1
        raise self.error

    def call_stream(self, call: ModelCall):
        self.stream_call_count += 1
        raise self.error


# 与 tests/unit/services/llm/test_resilience.py 同模式：假网关参数不注解，
# 容纳结构替身（重试只发生在真实网关内部，此处聚焦领域降级职责）。
def _embedding_service(gateway) -> EmbeddingService:
    return EmbeddingService(gateway=gateway, settings=_app_settings())


def _rerank_service(gateway, model_gateway: ModelGatewaySettings) -> RerankerService:
    return RerankerService(gateway=gateway, model_gateway=model_gateway, settings=_app_settings())


def _rewrite_service(gateway) -> QueryRewriteService:
    return QueryRewriteService(gateway=gateway, settings=_app_settings())


def _generation_service(gateway) -> GenerationService:
    return GenerationService(gateway=gateway, settings=_app_settings())


class TestCallTypeEnum:
    """第 1 条：call_type 恰为四类调用类型。"""

    def test_call_type_is_exactly_four_kinds(self) -> None:
        assert get_args(CallType) == ("embedding", "query_rewrite", "rerank", "generation")


class TestModelCallEnvelope:
    """第 2 条：调用信封最小字段与必填性（model-egress.md 调用信封表格）。"""

    def test_model_call_field_names_match_contract(self) -> None:
        assert _field_names(ModelCall) == {
            "call_id",
            "trace_id",
            "subject_digest",
            "call_type",
            "content",
            "options",
        }

    def test_model_call_required_fields_are_exactly_the_six_column_fields(self) -> None:
        by_name = {f.name: f for f in dataclasses.fields(ModelCall)}
        required = {
            name
            for name, f in by_name.items()
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        }
        assert required == {"call_id", "trace_id", "subject_digest", "call_type", "content"}

    def test_model_call_missing_required_field_rejected(self) -> None:
        # 调用信封为 frozen dataclass：缺必填字段直接 TypeError（无静默默认值）。
        with pytest.raises(TypeError):
            ModelCall(  # type: ignore[call-arg]
                trace_id="t-1", subject_digest="d" * 64, call_type="embedding", content="x"
            )
        with pytest.raises(TypeError):
            ModelCall(  # type: ignore[call-arg]
                call_id="c-1", trace_id="t-1", subject_digest="d" * 64, call_type="embedding"
            )

    def test_options_is_the_only_optional_field(self) -> None:
        call = ModelCall(
            call_id="c-1",
            trace_id="t-1",
            subject_digest="d" * 64,
            call_type="embedding",
            content="x",
        )
        assert call.options == {}

    def test_options_copied_from_caller_dict(self) -> None:
        # frozen 语义的完整承诺：构造后调用方修改自己的字典不得影响 ModelCall，
        # 网关/适配器读取的 options 必须与外部引用隔离（model-egress.md 调用信封）。
        caller_options: dict[str, Any] = {"candidate_count": 3}
        call = ModelCall(
            call_id="c-1",
            trace_id="t-1",
            subject_digest="d" * 64,
            call_type="rerank",
            content="x",
            options=caller_options,
        )
        assert call.options == {"candidate_count": 3}
        caller_options["candidate_count"] = 99
        caller_options["injected"] = "evil"
        assert call.options == {"candidate_count": 3}
        assert call.options is not caller_options

    def test_sanitized_call_options_copied_from_source(self) -> None:
        # 网关构造 SanitizedModelCall 时同样复制：源 dict 后续修改不影响适配层。
        source = {"candidate_count": 2}
        sanitized = SanitizedModelCall(
            call_id="c-1",
            trace_id="t-1",
            subject_digest="d" * 64,
            call_type="rerank",
            sanitization_status="passed",
            policy_version="v1",
            provider="openai-compatible",
            model="m",
            sanitized_content="x",
            options=source,
            timeout_seconds=10.0,
            max_retries=1,
        )
        assert sanitized.options == {"candidate_count": 2}
        source["candidate_count"] = 0
        assert sanitized.options == {"candidate_count": 2}


class TestNoCallerSpecifiedRouting:
    """第 3 条：业务调用方不得指定供应商凭证/endpoint/模型。"""

    def test_model_call_has_no_credential_endpoint_or_model_fields(self) -> None:
        names = _field_names(ModelCall)
        forbidden = {
            "api_key",
            "endpoint",
            "model",
            "provider",
            "headers",
            "timeout_seconds",
            "max_retries",
        }
        assert not (names & forbidden)

    def test_sanitized_call_has_no_credentials_but_gateway_assigned_routing(self) -> None:
        # SanitizedModelCall 由网关构造：携带供应商/模型/超时重试策略，但凭证
        # 只在网络发送边界注入（适配器构造），绝不出现在调用对象上。
        names = _field_names(SanitizedModelCall)
        assert not (names & {"api_key", "endpoint", "headers"})
        assert {"provider", "model", "timeout_seconds", "max_retries"} <= names


class TestSanitizedPreconditions:
    """第 4 条：脱敏状态与供应商适配层前置条件。"""

    def test_sanitized_call_status_literal_passed(self) -> None:
        hints = get_type_hints(SanitizedModelCall)
        assert get_args(hints["sanitization_status"]) == ("passed",)

    def test_sanitized_call_carries_required_precondition_fields(self) -> None:
        # 契约：必须携带 sanitization_status=passed、脱敏策略版本、调用类型、
        # 供应商、模型和超时/重试策略字段。
        names = _field_names(SanitizedModelCall)
        assert {
            "sanitization_status",
            "policy_version",
            "call_type",
            "provider",
            "model",
            "timeout_seconds",
            "max_retries",
        } <= names

    @pytest.mark.parametrize(
        "method",
        [
            ProviderAdapter.embed,
            ProviderAdapter.chat,
            ProviderAdapter.chat_stream,
            ProviderAdapter.rerank,
        ],
    )
    def test_adapter_interface_receives_only_sanitized_calls(self, method) -> None:
        # providers/base.py：适配器接口只收 SanitizedModelCall，不收业务 ModelCall。
        hints = get_type_hints(method)
        assert hints["call"] is SanitizedModelCall
        assert hints["call"] is not ModelCall

    def test_adapter_rejects_non_passed_call_before_network(self) -> None:
        # 供应商适配层前置条件：任何缺失或失败状态必须在发起网络连接前拒绝。
        invalid = _sanitized_call(sanitization_status="failed")
        with pytest.raises(GatewayError) as exc:
            OpenAICompatibleAdapter._require_passed(invalid)
        assert exc.value.error_class == "configuration"
        with pytest.raises(GatewayError) as exc:
            OpenAICompatibleAdapter._require_passed(_sanitized_call(model=""))
        assert exc.value.error_class == "configuration"

    def test_service_rejects_non_passed_call_with_zero_physical_requests(self, monkeypatch) -> None:
        adapter = _PreconditionAdapter()
        service = ModelGatewayService(_gateway_settings(), adapter=adapter)
        invalid = _sanitized_call(sanitization_status="failed")
        monkeypatch.setattr(service, "_build_sanitized", lambda *a, **k: invalid)
        with pytest.raises(GatewayError) as exc:
            service.call(_call("embedding"))
        assert exc.value.error_class == "configuration"
        assert adapter.embed_calls == 0  # 未标记已脱敏的调用对象不得产生任何物理请求


class TestEndpointAndProviderConfig:
    """第 5 条：endpoint 必填与 openai-compatible provider 规则。"""

    def test_endpoint_has_no_default_and_is_required_at_readiness(self) -> None:
        # 契约：MODEL_GATEWAY_ENDPOINT 无默认值且必须显式配置为合法 HTTPS base URL；
        # 缺省在就绪校验与工厂阶段拒绝（不得发起任何调用）。
        # 显式传 endpoint=None 覆盖测试环境注入（tests/conftest.py setdefault），
        # 使"未提供 endpoint"的场景可确定性复现。
        settings = ModelGatewaySettings(
            endpoint=None,
            api_key=SecretStr("sk-test"),
            query_rewrite_model="qr-model",
            generation_model="gen-model",
        )
        assert settings.endpoint is None
        errors = _validate_model_gateway(settings)
        assert "MODEL_GATEWAY_ENDPOINT is required" in errors
        with pytest.raises(ValueError, match="MODEL_GATEWAY_ENDPOINT"):
            build_provider_adapter(settings)

    def test_https_endpoint_accepted(self) -> None:
        assert (
            _gateway_settings(endpoint="https://api.example.com/v1").endpoint
            == "https://api.example.com/v1"
        )

    @pytest.mark.parametrize(
        "endpoint",
        ["http://localhost:8000/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"],
    )
    def test_loopback_http_endpoint_accepted(self, endpoint: str) -> None:
        # 仅本地开发/自动化测试允许 HTTP，且主机名必须精确为 localhost 或回环 IP。
        assert _gateway_settings(endpoint=endpoint).endpoint == endpoint

    def test_other_http_endpoint_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _gateway_settings(endpoint="http://api.example.com/v1")

    def test_invalid_scheme_endpoint_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _gateway_settings(endpoint="ftp://api.example.com/v1")

    def test_provider_defaults_to_openai_compatible_only(self) -> None:
        assert ModelGatewaySettings().provider == "openai-compatible"
        for provider in ("anthropic", "azure", "ollama"):
            with pytest.raises(ValidationError):
                ModelGatewaySettings(provider=provider)


class TestGatewaySoleRetryExecutor:
    """第 6、7 条：网关是超时/重试的唯一执行者，适配器单次物理请求。"""

    def test_retries_executed_only_by_gateway_budget(self) -> None:
        adapter = _CountingAdapter(error=APITimeoutError(request=None))  # type: ignore[arg-type]
        settings = _gateway_settings(embedding_max_retries=2)
        service = ModelGatewayService(settings, adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            service.call(_call("embedding"))
        assert exc.value.error_class == "timeout"
        # 每次适配器调用即一次物理请求：初次 + 2 次重试 = 1 + max_retries。
        assert adapter.embed_calls == 3

    def test_success_path_is_single_adapter_call(self) -> None:
        adapter = _CountingAdapter()
        service = ModelGatewayService(_gateway_settings(), adapter=adapter)
        service.call(_call("embedding"))
        assert adapter.embed_calls == 1

    def test_gateway_service_contains_the_retry_loops(self) -> None:
        # 结构断言：重试循环只存在于网关（call/call_stream 的 while 预算循环）。
        tree = ast.parse(inspect.getsource(ModelGatewayService))
        assert len([n for n in ast.walk(tree) if isinstance(n, ast.While)]) >= 2

    def test_openai_compatible_adapter_disables_client_builtin_retries(self) -> None:
        # 禁用 LangChain/底层客户端内建重试：超时与重试只由网关执行。
        assert '"max_retries": 0' in inspect.getsource(OpenAICompatibleAdapter._embeddings_client)
        assert '"max_retries": 0' in inspect.getsource(OpenAICompatibleAdapter._chat_client)


class TestBusinessAdaptersNoRetry:
    """第 6 条（结构侧）：业务适配器不实现重试，仅调用网关一次。"""

    def test_business_adapters_import_no_retry_library(self) -> None:
        for module in (embeddings_module, reranker_module, chat_module):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert all(
                        "tenacity" not in alias.name and "backoff" not in alias.name
                        for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert "tenacity" not in node.module and "backoff" not in node.module

    def test_business_adapters_have_no_retry_loops(self) -> None:
        for module in (embeddings_module, reranker_module, chat_module):
            tree = ast.parse(inspect.getsource(module))
            assert not any(isinstance(n, ast.While) for n in ast.walk(tree))

    def test_business_adapters_make_exactly_one_gateway_call_per_use_case(self) -> None:
        def count_gateway_calls(module) -> int:
            tree = ast.parse(inspect.getsource(module))
            return sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("call", "call_stream")
            )

        # 每个用例适配器只对网关发起一次调用（无第二层尝试）。
        assert count_gateway_calls(embeddings_module) == 1
        assert count_gateway_calls(reranker_module) == 1
        assert count_gateway_calls(chat_module) == 2  # 改写 call + 生成 call_stream


class TestFinalFailureAndDomainDegradation:
    """第 8 条：网关最终失败分类与业务领域降级职责分离。"""

    def test_gateway_returns_only_final_success_or_gateway_error(self) -> None:
        adapter = _CountingAdapter(error=APITimeoutError(request=None))  # type: ignore[arg-type]
        settings = _gateway_settings(query_rewrite_max_retries=1)
        service = ModelGatewayService(settings, adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            service.call(_call("query_rewrite"))
        # 网关只抛稳定分类的最终失败，不返回领域回退内容。
        assert exc.value.error_class in (
            "configuration",
            "sanitization_failed",
            "network",
            "provider_error",
            "timeout",
            "rate_limited",
            "invalid_response",
        )

    def test_embedding_final_failure_converges_domain_failure(self) -> None:
        gateway = _FailingGateway(GatewayError("sanitization_failed", "sanitization failed"))
        with pytest.raises(EmbeddingFailure):
            _embedding_service(gateway).embed_texts(["x"], user_id=uuid.uuid4())
        assert gateway.call_count == 1  # 不包装第二层重试

    def test_rerank_final_failure_falls_back_to_rrf(self) -> None:
        gateway = _FailingGateway(GatewayError("timeout", "timed out"))
        service = _rerank_service(gateway, ModelGatewaySettings(rerank_model="rr-model"))
        chunks = [
            RetrievalChunk(
                chunk_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                document_version=1,
                seq=0,
                content="候选0",
            )
        ]
        assert service.rerank_scores(user_id=uuid.uuid4(), query="问题", candidates=chunks) is None
        assert gateway.call_count == 1

    def test_rewrite_final_failure_falls_back_to_original_query(self) -> None:
        gateway = _FailingGateway(GatewayError("provider_error", "provider failed"))
        service = _rewrite_service(gateway)
        assert service.rewrite(user_id=uuid.uuid4(), query="原始问题", history=[]) == "原始问题"
        assert gateway.call_count == 1

    def test_generation_final_failure_converges_failed_not_cancel(self) -> None:
        gateway = _FailingGateway(GatewayError("timeout", "first token timeout"))
        service = _generation_service(gateway)
        with pytest.raises(GenerationFailure):
            list(service.stream(user_id=uuid.uuid4(), query="q", context_pack="c", history=[]))
        assert gateway.stream_call_count == 1


class TestRerankScoreContract:
    """第 9 条：Reranker scores[{candidate_index,score}] 完整性/整体回退。"""

    def test_rerank_score_fields_are_int_index_and_finite_float(self) -> None:
        hints = get_type_hints(RerankScore)
        assert hints["candidate_index"] is int
        assert hints["score"] is float
        score = RerankScore(candidate_index=2, score=0.5)
        assert score.candidate_index == 2
        assert math.isfinite(score.score)

    def test_valid_rerank_response_passes(self) -> None:
        raw = '{"scores":[{"candidate_index":1,"score":0.7},{"candidate_index":0,"score":0.9}]}'
        scores = OpenAICompatibleAdapter._parse_rerank_scores(raw, 2)
        assert [s.candidate_index for s in scores] == [1, 0]
        assert [s.score for s in scores] == [0.7, 0.9]

    @pytest.mark.parametrize(
        "raw",
        [
            # 非法 JSON / 空响应 / 顶层结构错误
            "",
            "not json",
            '{"scores":[]}',
            '{"extra":1,"scores":[{"candidate_index":0,"score":0.5}]}',
            '[{"candidate_index":0,"score":0.5}]',
            '{"scores":{"candidate_index":0,"score":0.5}}',
            # 字段缺失 / 字符串数字 / 多余项字段 / 布尔序号 / 布尔分数
            '{"scores":[{"candidate_index":0}]}',
            '{"scores":[{"candidate_index":0,"score":"high"}]}',
            '{"scores":[{"candidate_index":0,"score":0.5,"extra":1}]}',
            '{"scores":[{"candidate_index":true,"score":0.5}]}',
            '{"scores":[{"candidate_index":0,"score":true}]}',
            # 重复 / 越界 / 不完整（count=2）
            '{"scores":[{"candidate_index":0,"score":0.5},{"candidate_index":0,"score":0.4}]}',
            '{"scores":[{"candidate_index":5,"score":0.5}]}',
            '{"scores":[{"candidate_index":0,"score":0.5}]}',
            '{"scores":[{"candidate_index":0,"score":0.5},{"candidate_index":2,"score":0.3}]}',
            # 非有限数值：NaN / 正负无穷（json.loads 接受字面量，必须拒绝）
            '{"scores":[{"candidate_index":0,"score":NaN},{"candidate_index":1,"score":0.5}]}',
            '{"scores":[{"candidate_index":0,"score":Infinity},{"candidate_index":1,"score":0.5}]}',
            '{"scores":[{"candidate_index":0,"score":-Infinity},{"candidate_index":1,"score":0.5}]}',
        ],
    )
    def test_invalid_rerank_response_fails_whole(self, raw: str) -> None:
        # 任何非法响应整次重排失败（invalid_response），不返回部分评分。
        with pytest.raises(GatewayError) as exc:
            OpenAICompatibleAdapter._parse_rerank_scores(raw, 2)
        assert exc.value.error_class == "invalid_response"


class TestUnknownProviderRejected:
    """第 10 条：未知 provider 在 settings 与 factory 双层拒绝。"""

    def test_unknown_provider_rejected_in_settings(self) -> None:
        with pytest.raises(ValidationError):
            ModelGatewaySettings(provider="unknown-provider")

    def test_unknown_provider_rejected_in_factory(self) -> None:
        fake_settings = SimpleNamespace(
            provider="unknown-provider", endpoint="https://x", api_key=SecretStr("k")
        )
        with pytest.raises(ValueError, match="unknown model gateway provider"):
            build_provider_adapter(fake_settings)  # type: ignore[arg-type]


class TestAuditWhitelist:
    """第 11 条：日志元数据白名单与契约一致，记录函数不接受正文/凭证字段。"""

    def test_audit_whitelist_exact_set(self) -> None:
        assert set(ALLOWED_AUDIT_FIELDS) == EXPECTED_AUDIT_FIELDS

    def test_audit_record_fields_are_exactly_whitelist(self) -> None:
        names = {f.name for f in dataclasses.fields(ModelCallAudit)}
        names.add("duration_ms")  # 耗时由起止时间派生的属性
        assert names == EXPECTED_AUDIT_FIELDS

    def test_audit_record_has_no_payload_or_credential_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(ModelCallAudit)}
        forbidden = {
            "content",
            "prompt",
            "headers",
            "message",
            "filename",
            "api_key",
            "authorization",
        }
        assert not (names & forbidden)

    def test_audit_log_function_accepts_only_audit_metadata(self) -> None:
        params = inspect.signature(log_model_call).parameters
        assert list(params) == ["audit"]
        assert params["audit"].annotation is ModelCallAudit

    def test_whitelisted_event_never_contains_payload(self) -> None:
        audit = ModelCallAudit(
            trace_id="t",
            call_id="c",
            subject_digest="d",
            call_type="generation",
            provider="openai-compatible",
            model="gen-model",
            status="failed",
            started_at=0.0,
            error_class="timeout",
            retries=1,
        )
        event = audit.to_whitelisted()
        assert set(event) == EXPECTED_AUDIT_FIELDS
        assert "content" not in event and "prompt" not in event


class TestSanitizationContract:
    """第 12 条：脱敏规则（禁止字段删除、PII 不可逆占位符、fail-closed 零外发）。"""

    def test_forbidden_values_deleted_from_content(self) -> None:
        # 认证请求头：标签与值整段删除（贪婪匹配至行尾/分隔符）。
        result = sanitize_call(_call("generation", "Authorization: Bearer abc.def.ghi end"), "v1")
        assert result.content == "[REDACTED_AUTH]"
        for forbidden in ("Authorization", "Bearer", "abc.def.ghi"):
            assert forbidden not in result.content
        # URL 内嵌凭证：凭证部分删除，保留 URL 结构。
        result = sanitize_call(_call("generation", "see https://user:pass@example.com/x"), "v1")
        assert "user:pass" not in result.content
        assert "https://" in result.content
        # 内部绝对存储路径：路径本体替换为 [PATH]。
        result = sanitize_call(
            _call("generation", "stored at /data/orionamesh/upload/raw.pdf"), "v1"
        )
        assert "/data/orionamesh" not in result.content
        assert "[PATH]" in result.content
        # 用户/租户原始标识（UUID）：替换为 [ID]。
        result = sanitize_call(
            _call("generation", "user 3f2504e0-4f89-41d3-9a0c-0305e82c3301"), "v1"
        )
        assert "3f2504e0-4f89-41d3-9a0c-0305e82c3301" not in result.content
        assert "[ID]" in result.content

    def test_sensitive_option_keys_deleted(self) -> None:
        result = sanitize_call(
            _call(
                "question",
                password="p",
                token="t",
                api_key="k",
                authorization="Bearer x",
                cookie="c",
                headers={"x": "y"},
                keep=1,
            ),
            "v1",
        )
        assert result.options == {"keep": 1}

    def test_pii_replaced_with_irreversible_placeholders(self) -> None:
        text = "联系 alice@example.com 或 13800138000，证件 11010119900307789X"
        result = sanitize_call(_call("generation", text), "v1")
        for original in ("alice@example.com", "13800138000", "11010119900307789X"):
            assert original not in result.content
        assert "[EMAIL:" in result.content
        assert "[PHONE:" in result.content
        assert "[ID_CARD:" in result.content

    def test_placeholders_irreversible_and_not_persisted(self) -> None:
        # 每调用随机盐：同一原值在两次调用得到不同占位符，禁止持久化映射。
        r1 = sanitize_call(_call("generation", "mail a@b.co"), "v1")
        r2 = sanitize_call(_call("generation", "mail a@b.co"), "v1")
        assert r1.content != r2.content
        assert "a@b.co" not in r1.content and "a@b.co" not in r2.content

    def test_residual_credentials_fail_closed_zero_external_calls(self) -> None:
        adapter = _CountingAdapter()
        service = ModelGatewayService(_gateway_settings(), adapter=adapter)
        with pytest.raises(GatewayError) as exc:
            service.call(_call("embedding", content="leak token=abc123def"))
        assert exc.value.error_class == "sanitization_failed"
        assert adapter.embed_calls == 0  # 供应商假服务收到零请求

    def test_sanitizer_exception_fails_closed_zero_external_calls(self, monkeypatch) -> None:
        adapter = _CountingAdapter()
        service = ModelGatewayService(_gateway_settings(), adapter=adapter)

        def _boom(text: str) -> str:
            raise RuntimeError("sanitizer exploded")

        monkeypatch.setattr(
            "app.infrastructure.model_gateway.policies.v1.V1SanitizerPolicy.sanitize_text",
            _boom,
        )
        with pytest.raises(GatewayError) as exc:
            service.call(_call("embedding"))
        assert exc.value.error_class == "sanitization_failed"
        assert adapter.embed_calls == 0
