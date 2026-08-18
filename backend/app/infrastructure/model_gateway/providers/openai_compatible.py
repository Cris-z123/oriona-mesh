"""MVP ``openai-compatible`` 供应商适配器（T034 / model-egress.md）。

- 凭证只在发送边界（适配器构造）注入，绝不进入领域模型、任务 payload 或日志；
- 每次调用只执行一次物理供应商请求：Embedding 用 embeddings 端点，Query
  Rewrite/Generation 用 chat 端点，可选 Reranker 用 chat 端点返回结构化 scores；
- 禁用 LangChain/底层客户端内建重试（``max_retries=0``）：超时与重试只由网关执行，
  并由网关传入本次 timeout；
- Reranker 响应严格校验：顶层只允许 ``scores``，每项只允许
  ``candidate_index``/``score``，序号必须恰好出现一次且零基越界即失败，score 必须
  有限；任何解析/校验失败整次重排失败（:class:`GatewayError` invalid_response），
  不得返回部分评分。
"""

import json
import math
from collections.abc import Generator
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from app.infrastructure.model_gateway.types import (
    EmbeddingResult,
    GatewayError,
    GenerationResult,
    QueryRewriteResult,
    RerankResult,
    RerankScore,
    SanitizedModelCall,
)

_RERANK_PROMPT = (
    "Given the query and numbered candidate passages below, rank the candidates by "
    "relevance to the query. Respond with ONLY a JSON object of the form "
    '{"scores":[{"candidate_index":0,"score":0.0}]} where candidate_index is the '
    "zero-based index of each candidate passage and score is a finite number "
    "between 0 and 1. Every candidate index must appear exactly once."
)


class OpenAICompatibleAdapter:
    """通过 OpenAI-compatible 协议访问的供应商适配器。"""

    name = "openai-compatible"

    def __init__(self, endpoint: str, api_key: str) -> None:
        self._endpoint = endpoint
        self._api_key = api_key

    # ------------------------------------------------------------------
    # 四类调用
    # ------------------------------------------------------------------
    def embed(self, call: SanitizedModelCall) -> EmbeddingResult:
        self._require_passed(call)
        client = self._embeddings_client(call)
        vectors = client.embed_documents([call.sanitized_content])
        return EmbeddingResult(vectors=[list(map(float, v)) for v in vectors])

    def chat(self, call: SanitizedModelCall) -> QueryRewriteResult | GenerationResult:
        self._require_passed(call)
        client = self._chat_client(call)
        response = client.invoke([HumanMessage(content=call.sanitized_content)])
        content = response.content if isinstance(response.content, str) else ""
        finish_reason = (response.response_metadata or {}).get("finish_reason")
        if call.call_type == "generation":
            reason = "stop" if finish_reason == "stop" else "length"
            return GenerationResult(content=content, finish_reason=reason)
        return QueryRewriteResult(rewritten_query=content)

    def chat_stream(self, call: SanitizedModelCall) -> Generator[str, None, None]:
        self._require_passed(call)
        client = self._chat_client(call)
        for chunk in client.stream([HumanMessage(content=call.sanitized_content)]):
            text = chunk.content if isinstance(chunk.content, str) else ""
            if text:
                yield text

    def rerank(self, call: SanitizedModelCall) -> RerankResult:
        self._require_passed(call)
        client = self._chat_client(call)
        prompt = f"{_RERANK_PROMPT}\n\nQuery:\n{call.sanitized_content}"
        response = client.invoke([HumanMessage(content=prompt)])
        raw = response.content if isinstance(response.content, str) else ""
        raw = raw.strip()
        candidate_count = int(call.options.get("candidate_count", 0))
        scores = self._parse_rerank_scores(raw, candidate_count)
        return RerankResult(scores=scores)

    # ------------------------------------------------------------------
    # 客户端构造（凭证注入边界）
    # ------------------------------------------------------------------
    def _embeddings_client(self, call: SanitizedModelCall) -> OpenAIEmbeddings:
        # 构造参数为 langchain 运行时字段；kwargs 展开以避免类型桩缺失。
        kwargs: dict[str, Any] = {
            "model": call.model,
            "openai_api_base": self._endpoint,
            "openai_api_key": self._api_key,
            "request_timeout": call.timeout_seconds,
            "max_retries": 0,
        }
        return OpenAIEmbeddings(**kwargs)  # type: ignore[arg-type]

    def _chat_client(self, call: SanitizedModelCall) -> ChatOpenAI:
        kwargs: dict[str, Any] = {
            "model_name": call.model,
            "openai_api_base": self._endpoint,
            "openai_api_key": self._api_key,
            "request_timeout": call.timeout_seconds,
            "max_retries": 0,
            "temperature": 0,
        }
        return ChatOpenAI(**kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _require_passed(call: SanitizedModelCall) -> None:
        """供应商适配层前置条件：未标记为已脱敏的调用对象不得发起网络连接。"""
        if call.sanitization_status != "passed":
            raise GatewayError("configuration", "call is not sanitized")
        if not call.model:
            raise GatewayError("configuration", "model is required")

    @staticmethod
    def classify_exception(exc: Exception) -> GatewayError:
        """把供应商/底层异常转换为稳定失败分类；不携带原始正文。

        分类逻辑归属适配器（供应商 SDK 只允许出现在 providers/ 目录），
        网关通过 :meth:`ProviderAdapter.classify_exception` 端口消费。
        """
        if isinstance(exc, AuthenticationError):
            return GatewayError("configuration", "provider authentication failed")
        if isinstance(exc, RateLimitError):
            return GatewayError("rate_limited", "provider rate limited")
        if isinstance(exc, APITimeoutError):
            return GatewayError("timeout", "provider request timeout")
        if isinstance(exc, APIConnectionError):
            return GatewayError("network", "provider connection failed")
        if isinstance(exc, APIStatusError):
            return GatewayError("provider_error", "provider returned an error")
        if isinstance(exc, TimeoutError):
            return GatewayError("timeout", "request timeout")
        return GatewayError("provider_error", "provider call failed")

    @staticmethod
    def _parse_rerank_scores(raw: str, candidate_count: int) -> list[RerankScore]:
        if not raw:
            raise GatewayError("invalid_response", "rerank response is empty")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise GatewayError("invalid_response", "rerank response is not valid JSON") from None
        if not isinstance(data, dict) or set(data) != {"scores"}:
            raise GatewayError("invalid_response", "rerank response must only contain scores")
        items = data["scores"]
        if not isinstance(items, list):
            raise GatewayError("invalid_response", "rerank scores must be an array")
        parsed: list[RerankScore] = []
        seen: set[int] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"candidate_index", "score"}:
                raise GatewayError("invalid_response", "rerank score item has unexpected fields")
            index = item["candidate_index"]
            score = item["score"]
            if isinstance(index, bool) or not isinstance(index, int):
                raise GatewayError("invalid_response", "candidate_index must be an integer")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise GatewayError("invalid_response", "score must be a number")
            if not math.isfinite(float(score)):
                raise GatewayError("invalid_response", "score must be finite")
            if index < 0 or index >= candidate_count:
                raise GatewayError("invalid_response", "candidate_index out of range")
            if index in seen:
                raise GatewayError("invalid_response", "duplicate candidate_index")
            seen.add(index)
            parsed.append(RerankScore(candidate_index=index, score=float(score)))
        if len(seen) != candidate_count:
            raise GatewayError("invalid_response", "incomplete candidate scores")
        return parsed
