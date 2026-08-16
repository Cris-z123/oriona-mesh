"""统一模型出口编排服务（T035 / FR-027）。

将脱敏、路由、凭证、各调用类型超时/重试、稳定失败分类与元数据审计串联：
- 网关是模型调用超时与重试的唯一执行者；对业务调用方只返回最终成功或失败；
- 脱敏失败不得创建外部请求（fail-closed）；
- 不执行领域降级（Embedding 失败、改写原问题、RRF 回退、failed/error 收敛均由
  业务适配器负责）；
- 日志只记录白名单审计元数据，不含请求/响应正文。
"""

import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from app.infrastructure.model_gateway.audit import ModelCallAudit, log_model_call
from app.infrastructure.model_gateway.config import ModelGatewaySettings
from app.infrastructure.model_gateway.providers.base import ProviderAdapter
from app.infrastructure.model_gateway.providers.factory import build_provider_adapter
from app.infrastructure.model_gateway.sanitizer import SanitizationError, sanitize_call
from app.infrastructure.model_gateway.types import (
    CallType,
    GatewayError,
    GenerationDelta,
    ModelCall,
    ModelCallResult,
    SanitizedModelCall,
)

# 可重试的稳定失败分类；invalid_response/sanitization/configuration 不重试。
_RETRYABLE_CLASSES = ("network", "timeout", "rate_limited", "provider_error")


class ModelGatewayService:
    """统一模型出口网关实现。"""

    def __init__(
        self,
        settings: ModelGatewaySettings,
        adapter: ProviderAdapter | None = None,
        audit: Callable[[ModelCallAudit], None] = log_model_call,
    ) -> None:
        self._settings = settings
        self._adapter = adapter or build_provider_adapter(settings)
        self._audit = audit

    # ------------------------------------------------------------------
    # 非流式调用
    # ------------------------------------------------------------------
    def call(self, call: ModelCall) -> ModelCallResult:
        options = self._call_options(call.call_type)
        model = str(options["model"])
        started = time.time()
        try:
            sanitized_content = sanitize_call(call, self._settings.sanitizer_policy_version)
            sanitized = self._build_sanitized(call, sanitized_content, options, model)
        except SanitizationError as exc:
            raise GatewayError("sanitization_failed", "sanitization failed") from exc

        retries = int(options.get("max_retries", 0))
        attempt = 0
        while True:
            try:
                result = self._execute(sanitized)
                self._audit(
                    ModelCallAudit(
                        trace_id=call.trace_id,
                        call_id=call.call_id,
                        subject_digest=call.subject_digest,
                        call_type=call.call_type,
                        provider=self._adapter.name,
                        model=model,
                        status="success",
                        started_at=started,
                        retries=attempt,
                        payload_bytes=len(sanitized.sanitized_content.encode("utf-8")),
                    )
                )
                return result
            except GatewayError as exc:
                retryable = exc.error_class in _RETRYABLE_CLASSES and attempt < retries
                if not retryable:
                    self._audit(
                        ModelCallAudit(
                            trace_id=call.trace_id,
                            call_id=call.call_id,
                            subject_digest=call.subject_digest,
                            call_type=call.call_type,
                            provider=self._adapter.name,
                            model=model,
                            status="failed",
                            started_at=started,
                            error_class=exc.error_class,
                            retries=attempt,
                            payload_bytes=len(sanitized.sanitized_content.encode("utf-8")),
                        )
                    )
                    raise
                attempt += 1

    # ------------------------------------------------------------------
    # 流式生成调用（SSE 使用；首 token 超时与总时长超时由网关执行）
    # ------------------------------------------------------------------
    def call_stream(self, call: ModelCall) -> Iterator[GenerationDelta]:
        if call.call_type != "generation":
            raise GatewayError("configuration", "call_stream only supports generation")
        options = self._call_options("generation")
        model = str(options["model"])
        started = time.time()
        try:
            sanitized_content = sanitize_call(call, self._settings.sanitizer_policy_version)
            sanitized = self._build_sanitized(call, sanitized_content, options, model)
        except SanitizationError as exc:
            # 与 call() 一致：脱敏失败 fail-closed 且稳定分类。
            raise GatewayError("sanitization_failed", "sanitization failed") from exc

        retries = int(options.get("max_retries", 0))
        first_token_timeout = sanitized.first_token_timeout_seconds or sanitized.timeout_seconds
        total_timeout = sanitized.total_timeout_seconds
        attempt = 0
        while True:
            # 总时长从本次流式调用开始（首 token 等待前）计算并覆盖整个流：
            # 一次尝试的实际上限为 total_timeout 秒，首 token 等待只是其中更细的
            # 前置门槛，不得在 total_timeout 之外再叠加 first_token_timeout
            # （quickstart：MODEL_GATEWAY_GENERATION_TOTAL_TIMEOUT_SECONDS 生成总时长）。
            total_started = time.monotonic()
            try:
                chunks = self._adapter.chat_stream(sanitized)
                # 在重试循环内立即执行首个 next()：适配器生成器体在此执行，
                # 首块异常/超时才能被网关按预算重试（惰性返回会让重试空转）。
                first = self._first_chunk(chunks, first_token_timeout, total_started, total_timeout)
                break
            except GatewayError as exc:
                retryable = exc.error_class in _RETRYABLE_CLASSES and attempt < retries
                if not retryable:
                    self._audit(
                        ModelCallAudit(
                            trace_id=call.trace_id,
                            call_id=call.call_id,
                            subject_digest=call.subject_digest,
                            call_type=call.call_type,
                            provider=self._adapter.name,
                            model=model,
                            status="failed",
                            started_at=started,
                            error_class=exc.error_class,
                            retries=attempt,
                            payload_bytes=len(sanitized.sanitized_content.encode("utf-8")),
                        )
                    )
                    raise
                attempt += 1
        yield from self._stream_rest(
            chunks,
            first,
            call,
            model,
            sanitized,
            started,
            total_timeout,
            attempt,
            total_started,
        )

    # ------------------------------------------------------------------
    # 内部执行
    # ------------------------------------------------------------------
    def _execute(self, sanitized: SanitizedModelCall) -> ModelCallResult:
        try:
            if sanitized.call_type == "embedding":
                return self._adapter.embed(sanitized)
            if sanitized.call_type == "rerank":
                return self._adapter.rerank(sanitized)
            if sanitized.call_type in ("query_rewrite", "generation"):
                return self._adapter.chat(sanitized)
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 - 稳定分类后抛出，不泄漏正文
            raise self._adapter.classify_exception(exc) from exc
        raise GatewayError("configuration", f"unsupported call type: {sanitized.call_type}")

    def _stream_rest(
        self,
        chunks: Iterator[str],
        first: str | None,
        call: ModelCall,
        model: str,
        sanitized: SanitizedModelCall,
        started: float,
        total_timeout: float | None,
        retries_used: int,
        total_started: float,
    ) -> Iterator[GenerationDelta]:
        """消费首个增量之后的流；单 worker 线程 + 队列，总时长截止由网关执行。

        总时长从本次流式调用开始（含首 token 等待）持续计时，`total_started`
        由 :meth:`call_stream` 在调用适配器前设置；供应商中途停流或抛错时都能
        被收敛：队列读取带剩余时长超时，适配器异常经队列透传并按稳定分类重抛。
        """
        messages: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=8)

        def _produce() -> None:
            try:
                for chunk in chunks:
                    messages.put(("ok", chunk))
                messages.put(("eos", None))
            except BaseException as exc:  # noqa: BLE001 - 供应商异常透传主线程分类
                messages.put(("err", exc))

        threading.Thread(target=_produce, daemon=True).start()
        emitted = first is not None
        try:
            if first is not None:
                yield GenerationDelta(text=first)
            while True:
                remaining = None
                if total_timeout is not None:
                    remaining = total_timeout - (time.monotonic() - total_started)
                    if remaining <= 0:
                        raise GatewayError("timeout", "generation total timeout exceeded")
                try:
                    status, value = messages.get(timeout=remaining)
                except queue.Empty:
                    raise GatewayError("timeout", "generation total timeout exceeded") from None
                if status == "err":
                    exc = value
                    if isinstance(exc, GatewayError):
                        raise exc
                    raise self._adapter.classify_exception(exc) from exc
                if status == "eos":
                    break
                if value:
                    emitted = True
                    yield GenerationDelta(text=value)
        except GatewayError as exc:
            self._audit(
                ModelCallAudit(
                    trace_id=call.trace_id,
                    call_id=call.call_id,
                    subject_digest=call.subject_digest,
                    call_type=call.call_type,
                    provider=self._adapter.name,
                    model=model,
                    status="failed",
                    started_at=started,
                    error_class=exc.error_class,
                    retries=retries_used,
                    payload_bytes=len(sanitized.sanitized_content.encode("utf-8")),
                )
            )
            raise
        self._audit(
            ModelCallAudit(
                trace_id=call.trace_id,
                call_id=call.call_id,
                subject_digest=call.subject_digest,
                call_type=call.call_type,
                provider=self._adapter.name,
                model=model,
                status="success",
                started_at=started,
                retries=retries_used,
                payload_bytes=len(sanitized.sanitized_content.encode("utf-8")),
            )
        )
        yield GenerationDelta(text="", finish_reason="stop" if emitted else "length")

    def _first_chunk(
        self,
        chunks: Iterator[str],
        timeout: float,
        total_started: float,
        total_timeout: float | None,
    ) -> str | None:
        """在超时内取得首个文本增量并执行适配器生成器体。

        首 token 等待上限为 ``min(first_token_timeout, 剩余总时长)``：总时长
        覆盖整个流式调用（含首 token 等待），剩余预算耗尽时直接判 total
        timeout，不得让首 token 等待叠加在总时长之外。
        线程内异常必须透传（不吞）：供应商在首块前抛错时按网关稳定分类重抛，
        使重试循环能按预算收敛；超时抛 GatewayError(timeout)；空流返回 None。
        """
        if total_timeout is not None:
            remaining = total_timeout - (time.monotonic() - total_started)
            if remaining <= 0:
                raise GatewayError("timeout", "generation total timeout exceeded")
            timeout = min(timeout, remaining)
        outcome: list[tuple[str, Any]] = []

        def _pull() -> None:
            try:
                outcome.append(("ok", next(chunks)))
            except StopIteration:
                outcome.append(("eos", None))
            except BaseException as exc:  # noqa: BLE001 - 异常透传由调用方分类
                outcome.append(("err", exc))

        thread = threading.Thread(target=_pull, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise GatewayError("timeout", "generation first token timeout")
        status, value = outcome[0]
        if status == "err":
            exc = value
            if isinstance(exc, GatewayError):
                raise exc
            raise self._adapter.classify_exception(exc) from exc
        return value  # "ok" 时为 str；"eos" 时为 None

    # ------------------------------------------------------------------
    # 配置装配
    # ------------------------------------------------------------------
    def _call_options(self, call_type: CallType) -> dict[str, Any]:
        options = self._settings.to_call_options(call_type)
        # 调用方不得覆盖超时/重试（业务适配器只提供调用类型与输入）。
        if options.get("model") is None:
            raise GatewayError("configuration", f"model is not configured for {call_type}")
        return options

    def _build_sanitized(
        self, call: ModelCall, content, options: dict[str, Any], model: str
    ) -> SanitizedModelCall:
        return SanitizedModelCall(
            call_id=call.call_id,
            trace_id=call.trace_id,
            subject_digest=call.subject_digest,
            call_type=call.call_type,
            sanitization_status="passed",
            policy_version=self._settings.sanitizer_policy_version,
            provider=self._adapter.name,
            model=model,
            sanitized_content=content.content,
            options=content.options,
            # generation 无 timeout_seconds，网络请求超时对齐首 token 超时
            # （流式总时长由 total_timeout_seconds 在网关消费侧执行）。
            timeout_seconds=float(
                options.get("timeout_seconds") or options.get("first_token_timeout_seconds") or 0
            ),
            max_retries=int(options.get("max_retries", 0) or 0),
            first_token_timeout_seconds=(
                float(options["first_token_timeout_seconds"])
                if "first_token_timeout_seconds" in options
                else None
            ),
            total_timeout_seconds=(
                float(options["total_timeout_seconds"])
                if "total_timeout_seconds" in options
                else None
            ),
        )
