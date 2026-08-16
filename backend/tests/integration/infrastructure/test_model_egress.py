"""模型出口网关受控假供应商集成测试（T089 / model-egress.md 验证要求）。

在测试进程内启动本地 HTTP 假供应商（模拟 OpenAI-compatible 协议的
``/v1/embeddings`` 与 ``/v1/chat/completions`` 端点），记录全部收到的请求，验证：

1. 四类调用（embedding/query_rewrite/rerank/generation）全部经过内部出口网关
   （``app/infrastructure/model_gateway/service.py``），假供应商只收到网关发出的
   请求，业务层无法绕过；
2. 脱敏失败零网络请求（fail-closed）：脱敏器异常或禁止内容触发时假供应商收到
   请求数为 0；
3. 凭证边界：``Authorization`` 只在发送边界（适配器构造）注入；模型名与 endpoint
   由配置注入；业务调用方（ModelCall）无法指定凭证/endpoint/模型；
4. 网关超时重试与最终失败分类：假供应商前 N 次返回 500 或超时，网关按配置重试
   （请求计数 == 1 + 重试次数），最终失败分类稳定（timeout/provider_error），
   业务层不产生第二层请求；
5. 业务领域降级：网关最终失败后，Embedding 抛 :class:`EmbeddingFailure`、
   Query Rewrite 回退原问题、Reranker 回退原 RRF、Generation 抛
   :class:`GenerationFailure`，且均不重试；
6. 日志白名单：审计事件（``audit.py`` 的 :class:`ModelCallAudit` 与 structlog
   真实日志）只包含 :data:`ALLOWED_AUDIT_FIELDS`，不含请求/响应正文、凭证、
   提示词、问题、片段、文件名或供应商原始错误体。
"""

import json
import os
import threading
import time
import uuid
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

# 本机可能配置了系统代理（注册表，httpx 不识别注册表 bypass）；假供应商必须
# 直连回环地址，显式声明 NO_PROXY（httpx 在构造客户端时读取）。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.core.settings import Settings
from app.infrastructure.model_gateway.audit import ALLOWED_AUDIT_FIELDS, ModelCallAudit
from app.infrastructure.model_gateway.config import ModelGatewaySettings
from app.infrastructure.model_gateway.service import ModelGatewayService
from app.infrastructure.model_gateway.types import EmbeddingResult, GatewayError, ModelCall
from app.repositories.chunks import RetrievalChunk
from app.services.llm.chat import GenerationFailure, GenerationService, QueryRewriteService
from app.services.llm.embeddings import EMBEDDING_DIMENSION, EmbeddingFailure, EmbeddingService
from app.services.llm.reranker import RerankerService

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 受控假供应商：进程内 ThreadingHTTPServer，模拟 OpenAI-compatible 协议
# ---------------------------------------------------------------------------
class _FakeProvider:
    """记录全部请求并支持失败/超时注入的假供应商（``127.0.0.1:0`` 随机端口）。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail_until = 0  # 前 N 个请求返回 500（网关重试验证）
        self.always_fail = False  # 始终返回 500
        self.slow_seconds = 0.0  # 响应前 sleep 秒数（超时模拟）
        self.scores: list[dict[str, Any]] = [{"candidate_index": 0, "score": 0.9}]
        self.rewritten_query = "改写后的查询"
        self.generation_chunks = ["根据", "知识库", "回答"]
        self.endpoint = ""
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProviderHandler)
        # handler 通过 self.server.provider 访问共享状态（运行时属性注入）。
        self._httpd.provider = self  # type: ignore[attr-defined]
        self.endpoint = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    @property
    def request_count(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]


class _FakeProviderHandler(BaseHTTPRequestHandler):
    """假端点：按路径区分 ``/v1/embeddings`` 与 ``/v1/chat/completions``。

    ``/chat/completions`` 对 rerank 请求（提示以 ``Given the query`` 开头）返回
    结构化 ``scores`` JSON，对流式请求（``stream: true``）返回 SSE 增量。
    """

    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        """抑制请求日志噪音，避免测试输出被访问日志污染。"""

    def do_POST(self) -> None:
        provider: _FakeProvider = cast(Any, self.server).provider
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        provider.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        try:
            if provider.slow_seconds:
                time.sleep(provider.slow_seconds)
            if provider.always_fail or len(provider.requests) <= provider.fail_until:
                self._send_json(
                    500,
                    {
                        "error": {
                            "message": "模拟供应商失败",
                            "type": "server_error",
                            "code": "internal_error",
                        }
                    },
                )
                return
            payload = json.loads(body)
            if self.path.endswith("/v1/embeddings"):
                self._handle_embeddings(payload)
            elif self.path.endswith("/v1/chat/completions"):
                self._handle_chat(payload)
            else:
                self._send_json(
                    404,
                    {
                        "error": {
                            "message": "not found",
                            "type": "invalid_request_error",
                            "code": "not_found",
                        }
                    },
                )
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_embeddings(self, payload: dict[str, Any]) -> None:
        input_items = payload.get("input", [])
        data = [
            {"object": "embedding", "embedding": [0.1] * EMBEDDING_DIMENSION, "index": i}
            for i in range(len(input_items))
        ]
        self._send_json(200, {"object": "list", "data": data, "model": payload.get("model", "")})

    def _handle_chat(self, payload: dict[str, Any]) -> None:
        provider: _FakeProvider = cast(Any, self.server).provider
        if payload.get("stream"):
            self._send_stream(provider, payload)
            return
        content = str(payload["messages"][0]["content"])
        if content.startswith("Given the query"):
            answer = json.dumps({"scores": provider.scores})
        else:
            answer = provider.rewritten_query
        self._send_json(
            200,
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": payload.get("model", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def _send_stream(self, provider: _FakeProvider, payload: dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for text in provider.generation_chunks:
            chunk = {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": payload.get("model", ""),
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        done = {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": payload.get("model", ""),
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }
        self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_provider() -> Generator[_FakeProvider, None, None]:
    provider = _FakeProvider()
    provider.start()
    yield provider
    provider.stop()


# ---------------------------------------------------------------------------
# 装配辅助
# ---------------------------------------------------------------------------
def _gateway_settings(provider: _FakeProvider, **overrides: Any) -> ModelGatewaySettings:
    """指向假供应商的网关配置；显式参数覆盖 conftest 环境变量。"""
    base: dict[str, Any] = {
        "endpoint": provider.endpoint,
        "api_key": SecretStr("test-api-key"),
        "query_rewrite_model": "rewrite-model",
        "generation_model": "gen-model",
        "rerank_model": "rerank-model",
    }
    base.update(overrides)
    return ModelGatewaySettings(**base)


def _gateway(
    provider: _FakeProvider,
    *,
    audit: Any = None,
    **overrides: Any,
) -> ModelGatewayService:
    settings = _gateway_settings(provider, **overrides)
    if audit is None:
        return ModelGatewayService(settings)
    return ModelGatewayService(settings, audit=audit)


def _call(call_type: str, content: str = "普通文本", **options: Any) -> ModelCall:
    return ModelCall(
        call_id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        subject_digest="d" * 64,
        call_type=call_type,  # type: ignore[arg-type]
        content=content,
        options=options,
    )


def _chunks(n: int = 2) -> list[RetrievalChunk]:
    return [
        RetrievalChunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_version=1,
            seq=i,
            content=f"候选{i}文本",
        )
        for i in range(n)
    ]


def _app_settings() -> Settings:
    """业务适配器使用的应用设置（conftest 已装配测试环境变量）。"""
    return Settings()


# ---------------------------------------------------------------------------
# 1. 四类调用全部经过网关，假供应商只收到网关发出的请求
# ---------------------------------------------------------------------------
class TestAllCallTypesThroughGateway:
    def test_embedding_goes_through_gateway(self, fake_provider: _FakeProvider) -> None:
        service = EmbeddingService(gateway=_gateway(fake_provider), settings=_app_settings())
        vectors = service.embed_texts(["片段文本"], user_id=uuid.uuid4())
        assert len(vectors) == 1
        assert len(vectors[0]) == EMBEDDING_DIMENSION
        assert fake_provider.request_count == 1
        assert fake_provider.last["method"] == "POST"
        assert fake_provider.last["path"] == "/v1/embeddings"
        assert json.loads(fake_provider.last["body"])["model"] == "text-embedding-3-small"

    def test_query_rewrite_goes_through_gateway(self, fake_provider: _FakeProvider) -> None:
        service = QueryRewriteService(gateway=_gateway(fake_provider), settings=_app_settings())
        result = service.rewrite(
            user_id=uuid.uuid4(), query="原始问题", history=[("user", "上一问")]
        )
        assert result == fake_provider.rewritten_query
        assert fake_provider.request_count == 1
        assert fake_provider.last["path"] == "/v1/chat/completions"
        assert json.loads(fake_provider.last["body"])["model"] == "rewrite-model"

    def test_rerank_goes_through_gateway(self, fake_provider: _FakeProvider) -> None:
        fake_provider.scores = [
            {"candidate_index": 1, "score": 0.9},
            {"candidate_index": 0, "score": 0.4},
        ]
        gateway = _gateway(fake_provider)
        service = RerankerService(
            gateway=gateway,
            model_gateway=_gateway_settings(fake_provider),
            settings=_app_settings(),
        )
        scores = service.rerank_scores(user_id=uuid.uuid4(), query="问题", candidates=_chunks(2))
        assert scores is not None
        assert [s.candidate_index for s in scores] == [1, 0]
        assert fake_provider.request_count == 1
        assert fake_provider.last["path"] == "/v1/chat/completions"

    def test_generation_streams_through_gateway(self, fake_provider: _FakeProvider) -> None:
        service = GenerationService(gateway=_gateway(fake_provider), settings=_app_settings())
        deltas = list(
            service.stream(user_id=uuid.uuid4(), query="问题", context_pack="上下文", history=[])
        )
        assert [d.text for d in deltas if d.text] == fake_provider.generation_chunks
        assert fake_provider.request_count == 1
        assert fake_provider.last["path"] == "/v1/chat/completions"
        assert json.loads(fake_provider.last["body"])["stream"] is True


# ---------------------------------------------------------------------------
# 2. 脱敏失败零网络请求（fail-closed）
# ---------------------------------------------------------------------------
class TestSanitizationFailClosed:
    def test_sanitizer_raise_produces_zero_requests(
        self, fake_provider: _FakeProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.infrastructure.model_gateway import sanitizer

        def _boom(call: ModelCall, policy_version: str) -> None:
            raise sanitizer.SanitizationError("forced sanitization failure")

        monkeypatch.setattr("app.infrastructure.model_gateway.service.sanitize_call", _boom)
        gateway = _gateway(fake_provider)
        with pytest.raises(GatewayError) as exc:
            gateway.call(_call("embedding"))
        assert exc.value.error_class == "sanitization_failed"
        assert fake_provider.request_count == 0

    def test_forbidden_content_triggers_fail_closed_zero_requests(
        self, fake_provider: _FakeProvider
    ) -> None:
        gateway = _gateway(fake_provider)
        with pytest.raises(GatewayError) as exc:
            gateway.call(_call("query_rewrite", content="普通文本 password=secret123"))
        assert exc.value.error_class == "sanitization_failed"
        assert fake_provider.request_count == 0

    def test_stream_sanitization_failure_zero_requests(self, fake_provider: _FakeProvider) -> None:
        gateway = _gateway(fake_provider)
        with pytest.raises(GatewayError) as exc:
            list(gateway.call_stream(_call("generation", content="普通文本 token=abc123")))
        assert exc.value.error_class == "sanitization_failed"
        assert fake_provider.request_count == 0


# ---------------------------------------------------------------------------
# 3. 凭证边界：Authorization 只在发送边界注入；模型/endpoint 由配置决定
# ---------------------------------------------------------------------------
class TestCredentialBoundary:
    def test_call_cannot_override_credentials_model_or_endpoint(
        self, fake_provider: _FakeProvider
    ) -> None:
        gateway = _gateway(fake_provider)
        # 业务调用方试图通过 options 携带凭证/endpoint/模型：全部被忽略。
        gateway.call(
            _call(
                "embedding",
                options={
                    "api_key": "hijacked-key",
                    "authorization": "Bearer hijacked",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "model": "hijack-model",
                },
            )
        )
        assert fake_provider.request_count == 1
        request = fake_provider.last
        assert request["headers"]["authorization"] == "Bearer test-api-key"
        assert request["path"] == "/v1/embeddings"
        assert json.loads(request["body"])["model"] == "text-embedding-3-small"

    def test_model_and_endpoint_injected_from_config(self, fake_provider: _FakeProvider) -> None:
        gateway = _gateway(fake_provider, embedding_model="custom-embed-model")
        gateway.call(_call("embedding"))
        assert fake_provider.last["path"] == "/v1/embeddings"
        assert json.loads(fake_provider.last["body"])["model"] == "custom-embed-model"

    def test_chat_requests_carry_configured_authorization(
        self, fake_provider: _FakeProvider
    ) -> None:
        gateway = _gateway(fake_provider)
        gateway.call(_call("query_rewrite"))
        request = fake_provider.last
        assert request["path"] == "/v1/chat/completions"
        assert request["headers"]["authorization"] == "Bearer test-api-key"
        assert json.loads(request["body"])["model"] == "rewrite-model"


# ---------------------------------------------------------------------------
# 4. 网关超时重试与最终失败分类（请求计数 == 1 + 重试次数，无业务层第二层请求）
# ---------------------------------------------------------------------------
class TestRetryAndTimeout:
    def test_retries_on_provider_500_until_success(self, fake_provider: _FakeProvider) -> None:
        fake_provider.fail_until = 1  # 前 1 次失败，第 2 次成功
        gateway = _gateway(fake_provider, embedding_max_retries=2)
        result = gateway.call(_call("embedding"))
        assert isinstance(result, EmbeddingResult)
        assert len(result.vectors) == 1
        assert fake_provider.request_count == 2  # 1 次失败 + 1 次重试

    def test_retry_budget_exhausted_final_provider_error(
        self, fake_provider: _FakeProvider
    ) -> None:
        fake_provider.always_fail = True
        gateway = _gateway(fake_provider, embedding_max_retries=2)
        with pytest.raises(GatewayError) as exc:
            gateway.call(_call("embedding"))
        assert exc.value.error_class == "provider_error"
        assert fake_provider.request_count == 3  # 初始 + 2 次重试

    def test_timeout_retried_by_gateway_and_final_timeout(
        self, fake_provider: _FakeProvider
    ) -> None:
        fake_provider.slow_seconds = 3  # 超过 1 秒超时
        gateway = _gateway(fake_provider, embedding_timeout_seconds=1, embedding_max_retries=1)
        with pytest.raises(GatewayError) as exc:
            gateway.call(_call("embedding"))
        assert exc.value.error_class == "timeout"
        assert fake_provider.request_count == 2  # 初始 + 1 次重试

    def test_stream_first_token_timeout_retry_budget(self, fake_provider: _FakeProvider) -> None:
        fake_provider.slow_seconds = 3  # 首块前停流超过首 token 超时
        gateway = _gateway(
            fake_provider,
            generation_first_token_timeout_seconds=1,
            generation_total_timeout_seconds=30,
            generation_max_retries=1,
        )
        with pytest.raises(GatewayError) as exc:
            list(gateway.call_stream(_call("generation")))
        assert exc.value.error_class == "timeout"
        assert fake_provider.request_count == 2  # 初始 + 1 次重试

    def test_stream_provider_500_retry_budget_exhausted(self, fake_provider: _FakeProvider) -> None:
        fake_provider.always_fail = True
        gateway = _gateway(fake_provider, generation_max_retries=1)
        with pytest.raises(GatewayError) as exc:
            list(gateway.call_stream(_call("generation")))
        assert exc.value.error_class == "provider_error"
        assert fake_provider.request_count == 2  # 初始 + 1 次重试


# ---------------------------------------------------------------------------
# 5. 业务领域降级（网关最终失败后，业务适配器收敛且不重试）
# ---------------------------------------------------------------------------
class TestBusinessDegradation:
    def test_embedding_final_failure_fails_task(self, fake_provider: _FakeProvider) -> None:
        fake_provider.always_fail = True
        service = EmbeddingService(gateway=_gateway(fake_provider), settings=_app_settings())
        with pytest.raises(EmbeddingFailure):
            service.embed_texts(["片段文本"], user_id=uuid.uuid4())
        # 网关预算：初始 1 + 默认 2 次重试；业务层不产生第二层请求。
        assert fake_provider.request_count == 3

    def test_query_rewrite_final_failure_falls_back_to_original(
        self, fake_provider: _FakeProvider
    ) -> None:
        fake_provider.always_fail = True
        service = QueryRewriteService(gateway=_gateway(fake_provider), settings=_app_settings())
        query = "原始问题"
        assert service.rewrite(user_id=uuid.uuid4(), query=query, history=[]) == query
        assert fake_provider.request_count == 2  # 初始 + 默认 1 次重试

    def test_rerank_final_failure_falls_back_to_rrf(self, fake_provider: _FakeProvider) -> None:
        fake_provider.always_fail = True
        service = RerankerService(
            gateway=_gateway(fake_provider),
            model_gateway=_gateway_settings(fake_provider),
            settings=_app_settings(),
        )
        assert (
            service.rerank_scores(user_id=uuid.uuid4(), query="问题", candidates=_chunks(2)) is None
        )
        assert fake_provider.request_count == 2  # 初始 + 默认 1 次重试

    def test_generation_final_failure_raises_generation_failure(
        self, fake_provider: _FakeProvider
    ) -> None:
        fake_provider.always_fail = True
        service = GenerationService(gateway=_gateway(fake_provider), settings=_app_settings())
        with pytest.raises(GenerationFailure):
            list(
                service.stream(
                    user_id=uuid.uuid4(), query="问题", context_pack="上下文", history=[]
                )
            )
        assert fake_provider.request_count == 2  # 初始 + 默认 1 次重试


# ---------------------------------------------------------------------------
# 6. 日志白名单：只记录 ALLOWED_AUDIT_FIELDS，无正文/凭证/提示词/片段/错误体
# ---------------------------------------------------------------------------
class TestAuditWhitelist:
    def test_audit_event_contains_only_whitelisted_fields(
        self, fake_provider: _FakeProvider
    ) -> None:
        collected: list[ModelCallAudit] = []
        gateway = _gateway(fake_provider, audit=collected.append)
        gateway.call(_call("query_rewrite", content="普通问题"))
        assert len(collected) == 1
        audit = collected[0]
        assert set(audit.to_whitelisted()) == set(ALLOWED_AUDIT_FIELDS)
        assert audit.status == "success"
        assert audit.error_class is None
        # 禁止字段不得出现在审计对象上。
        for forbidden in ("content", "prompt", "payload", "headers", "api_key", "authorization"):
            assert not hasattr(audit, forbidden)

    def test_structlog_events_only_whitelisted_keys(self, fake_provider: _FakeProvider) -> None:
        gateway = _gateway(fake_provider)  # 默认审计 = log_model_call
        with capture_logs() as cap:
            gateway.call(_call("query_rewrite", content="普通问题"))
        assert len(cap) == 1
        event = cap[0]
        assert event["event"] == "model_gateway_call"
        assert set(event) <= set(ALLOWED_AUDIT_FIELDS) | {"event", "log_level"}
        assert "普通问题" not in json.dumps(event, ensure_ascii=False)

    def test_failure_logs_never_contain_bodies_or_provider_errors(
        self, fake_provider: _FakeProvider
    ) -> None:
        # 供应商 500 失败：错误体、问题原文不得进入日志。
        fake_provider.always_fail = True
        gateway = _gateway(fake_provider, query_rewrite_max_retries=0)
        with capture_logs() as cap:
            with pytest.raises(GatewayError):
                gateway.call(_call("query_rewrite", content="普通问题"))
        assert len(cap) == 1
        rendered = json.dumps(cap[0], ensure_ascii=False)
        assert "模拟供应商失败" not in rendered
        assert "普通问题" not in rendered
        assert set(cap[0]) <= set(ALLOWED_AUDIT_FIELDS) | {"event", "log_level"}
        assert cap[0]["status"] == "failed"
        assert cap[0]["error_class"] == "provider_error"

    def test_sanitization_failure_logs_metadata_without_forbidden_content(
        self, fake_provider: _FakeProvider
    ) -> None:
        # 脱敏失败 fail-closed：不产生审计事件（不记录任何含原文的内容）、
        # 零网络请求，异常分类稳定为 sanitization_failed。
        collected: list[ModelCallAudit] = []
        gateway = _gateway(fake_provider, audit=collected.append, query_rewrite_max_retries=0)
        with capture_logs() as cap:
            with pytest.raises(GatewayError) as exc:
                gateway.call(_call("query_rewrite", content="普通文本 password=secret123"))
        assert exc.value.error_class == "sanitization_failed"
        assert fake_provider.request_count == 0
        assert collected == []
        assert cap == []
