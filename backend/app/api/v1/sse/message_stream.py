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
  取消并置位停止信号：生产者中止拉取、关闭生成流（级联关闭网关侧供应商流），
  不再让后台线程阻塞在满队列上持续消耗供应商连接与配额；迟到的完成/失败写入
  由终态单向收敛器忽略，不会覆盖已收敛的终态。
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
# 生产者停止事件轮询入队的等待上限：消费方放弃后能及时感知 stop 并退出。
_QUEUE_PUT_TIMEOUT = 0.2


def _put_with_stop(
    messages: queue.Queue[tuple[str, GenerationDelta | None]],
    item: tuple[str, GenerationDelta | None],
    stop: threading.Event,
) -> bool:
    """带停止检查的入队：入队成功返回 True；stop 置位后放弃入队返回 False。

    消费协程放弃（客户端断开）后，生产者不得永久阻塞在满队列上：每次入队
    等待带超时，期间检查停止事件，置位即退出（由调用方关闭生成流）。
    """
    while not stop.is_set():
        try:
            messages.put(item, timeout=_QUEUE_PUT_TIMEOUT)
            return True
        except queue.Full:
            continue
    return False


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
    """后台线程消费生成流；任何失败（含未分类异常）收敛 failed/error 并中止。

    消费方退出（正常 eos/客户端断开关闭生成器）时在 ``finally`` 置位停止信号：
    生产者不再阻塞于满队列，退出前关闭生成流，级联终止网关侧的供应商请求；
    同时投递终止哨兵，解除仍阻塞在 ``queue.get`` 的 ``asyncio.to_thread``
    工作线程（不投递则断连会永久占用线程池槽位）。
    """
    messages: queue.Queue[tuple[str, GenerationDelta | None]] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    stop = threading.Event()

    def _produce() -> None:
        gen = answer.stream_generation(bundle)
        try:
            for delta in gen:
                if not _put_with_stop(messages, ("delta", delta), stop):
                    return
            _put_with_stop(messages, ("eos", None), stop)
        except GenerationFailure:
            _put_with_stop(messages, ("err", None), stop)
        except Exception as exc:  # noqa: BLE001 - 未分类服务异常同样收敛，不悬挂流
            logger.warning("sse_generation_producer_error", extra={"exc_type": type(exc).__name__})
            _put_with_stop(messages, ("err", None), stop)
        finally:
            if stop.is_set():
                # 消费方已放弃（客户端断开）：关闭生成流，级联终止网关侧供应商
                # 请求；随后投递终止哨兵解除阻塞中的消费线程（队列满时不存在
                # 阻塞中的 get，哨兵无必要）。
                try:
                    gen.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响已收敛的取消
                    pass
                try:
                    messages.put(("stop", None), timeout=_QUEUE_PUT_TIMEOUT)
                except queue.Full:
                    pass

    threading.Thread(target=_produce, daemon=True).start()
    try:
        while True:
            status, value = await asyncio.to_thread(messages.get)
            if status == "err":
                answer.fail(bundle.message_id, user_id=bundle.user_id)
                raise _StreamError()
            if status == "eos":
                return
            yield cast(GenerationDelta, value)
    finally:
        stop.set()
