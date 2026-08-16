"""SSE 消息流（T073 / openapi.yaml messages 段 x-sse-event-schema）。

- 原始文本帧使用 ``event: {name}\\ndata: {json}\\n\\n`` 线格式，data 为统一响应
  信封的 JSON（``code/data/msg/trace_id``）；
- 五类判别事件：``message_start``、``retrieval_done``、``delta``、``message_end``、
  ``error``；``retrieval_done`` 与引用详情复用同一 Citation 字段语义；
- 终态语义：正常完成及可信无证据为 ``completed/stop``，供应商/模型/服务错误
  重试耗尽发送 ``error`` 并收敛 ``failed/error``，客户端连接断开收敛
  ``cancelled/cancelled``；任何分支都不遗留 ``streaming``；
- 生成在后台线程消费（网关只执行超时与重试），主协程经有界队列驱动；生产者
  任何未分类异常都发送 ``err`` 哨兵，消费方收敛 ``failed/error`` 并发送
  ``error`` 帧，流不会悬挂；客户端断开时异步生成器被关闭，``finally`` 收敛
  取消；迟到的完成/失败写入由终态单向收敛器忽略，不会覆盖已收敛的终态。
"""

import asyncio
import json
import logging
import queue
import threading
from collections.abc import AsyncIterator
from typing import cast

from app.api.middleware.trace import TRACE_ID_VAR
from app.api.v1.schemas.common import DEFAULT_ERROR_MSG
from app.infrastructure.model_gateway.types import GenerationDelta
from app.services.answer_service import AnswerService, EvidenceBundle, NoEvidenceAnswer
from app.services.llm.chat import GenerationFailure

logger = logging.getLogger(__name__)

_STREAM_ERROR_CODE = 50000
# 生成线程与消费协程之间的有界队列：客户端停滞时提供背压，避免无限缓冲。
_QUEUE_MAXSIZE = 8


def _success_frame(event: str, data: dict, trace_id: str) -> str:
    envelope = {"code": 0, "data": data, "msg": "", "trace_id": trace_id}
    return f"event: {event}\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"


def _error_frame(code: int, msg: str, trace_id: str) -> str:
    envelope = {"code": code, "data": None, "msg": msg, "trace_id": trace_id}
    return f"event: error\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"


async def stream_answer_events(
    answer: AnswerService, bundle: NoEvidenceAnswer | EvidenceBundle
) -> AsyncIterator[str]:
    """把问答编排流转换为 SSE 文本帧；assistant 终态只收敛一次。"""
    trace_id = TRACE_ID_VAR.get() or ""
    converged = False
    try:
        yield _success_frame("message_start", {"message_id": str(bundle.message_id)}, trace_id)
        previews = bundle.citation_previews() if isinstance(bundle, EvidenceBundle) else []
        yield _success_frame("retrieval_done", {"citations": previews}, trace_id)
        if isinstance(bundle, NoEvidenceAnswer):
            # 可信无证据：prepare 已收敛 completed/stop，只发送 message_end。
            converged = True
            yield _success_frame(
                "message_end",
                {"message_id": str(bundle.message_id), "finish_reason": "stop"},
                trace_id,
            )
            return

        collected: list[str] = []
        final_reason = "stop"
        try:
            async for delta in _stream_generation(answer, bundle):
                if delta.finish_reason:
                    # 终结增量（携带 finish_reason）不发送 delta 帧，message_end 表达终态。
                    final_reason = delta.finish_reason
                    continue
                if not delta.text:
                    continue  # 空增量不产生帧（delta 文本至少 1 字符）
                collected.append(delta.text)
                yield _success_frame("delta", {"text": delta.text}, trace_id)
        except _StreamError:
            # 生成最终失败（含未分类服务异常）已收敛 failed/error，发送 error 事件。
            converged = True
            yield _error_frame(_STREAM_ERROR_CODE, DEFAULT_ERROR_MSG, trace_id)
            return
        answer.complete(bundle, content="".join(collected), finish_reason=final_reason)
        converged = True
        yield _success_frame(
            "message_end",
            {"message_id": str(bundle.message_id), "finish_reason": final_reason},
            trace_id,
        )
    finally:
        if not converged:
            # 客户端断开或未分类异常：收敛 cancelled/cancelled（单向，不覆盖终态）。
            answer.cancel(bundle.message_id, user_id=bundle.user_id)


class _StreamError(Exception):
    """生成最终失败已收敛 failed/error；终止流但不触发取消收敛。"""


async def _stream_generation(answer: AnswerService, bundle: EvidenceBundle):
    """后台线程消费生成流；任何失败（含未分类异常）收敛 failed/error 并中止。"""
    messages: queue.Queue[tuple[str, GenerationDelta | None]] = queue.Queue(maxsize=_QUEUE_MAXSIZE)

    def _produce() -> None:
        try:
            for delta in answer.stream_generation(bundle):
                messages.put(("delta", delta))
            messages.put(("eos", None))
        except GenerationFailure:
            messages.put(("err", None))
        except Exception as exc:  # noqa: BLE001 - 未分类服务异常同样收敛，不悬挂流
            logger.warning("sse_generation_producer_error", extra={"exc_type": type(exc).__name__})
            messages.put(("err", None))

    threading.Thread(target=_produce, daemon=True).start()
    while True:
        status, value = await asyncio.to_thread(messages.get)
        if status == "err":
            answer.fail(bundle.message_id, user_id=bundle.user_id)
            raise _StreamError()
        if status == "eos":
            return
        yield cast(GenerationDelta, value)
