"""统一安全解析包装（T051 / data-model.md 解析边界）。

- 解析在独立子进程（``spawn`` 上下文）中执行：超时后终止子进程，恶意或异常
  文件无法越过解析时长边界持续占用 CPU/内存（线程无法被强制终止，仅收敛状态
  会绕过资源边界）；超时收敛为 ``20001``；
- 解析器对象只以 ``module.QualName`` 描述跨进程传递，子进程内零参重建，不
  携带解析器实例状态；仅支持模块级、可无参构造的解析器类；
- 空/纯空白标准化文本收敛为 ``20010 EMPTY_DOCUMENT``，不得进入分块阶段；
- 解析器未知异常统一映射 ``20001`` 固定安全提示，不泄漏内部细节。
"""

import importlib
import multiprocessing
from queue import Empty
from typing import Any

from app.services.parsers.base import (
    EMPTY_DOC_CODE,
    EMPTY_DOC_MSG,
    PARSE_FAILED_CODE,
    PARSE_FAILED_MSG,
    ParseError,
    ParseOutput,
    parse_failed,
)

# 终止子进程后等待其回收的上限。
_PROCESS_TERMINATE_GRACE_SECONDS = 10.0


def _parser_ref(parser: Any) -> str:
    """把解析器实例序列化为模块级类引用（子进程内可重建）。"""
    cls = type(parser)
    if cls.__module__ == "__main__" or cls.__qualname__ != cls.__name__:
        raise ValueError(f"parser must be a module-level class: {cls!r}")
    return f"{cls.__module__}.{cls.__name__}"


def _load_parser(ref: str) -> Any:
    """在子进程内按模块级类引用重建解析器实例。"""
    module_name, _, class_name = ref.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


def _parse_child(parser_ref: str, content: bytes, max_expanded_bytes: int | None, queue) -> None:
    """子进程执行体：重建解析器 → 解析 → 单次投递结果或稳定错误。"""
    parser = _load_parser(parser_ref)
    try:
        result = parser.parse(content, max_expanded_bytes=max_expanded_bytes)
    except ParseError as exc:
        queue.put(("error", exc))
    except Exception:
        queue.put(("error", parse_failed()))
    else:
        queue.put(("result", result))
    finally:
        # 等待 feeder 线程把结果刷入管道，父进程 get() 不会因进程退出丢数据。
        queue.close()
        queue.join_thread()


def parse_safely(
    parser: Any,
    content: bytes,
    *,
    timeout_seconds: float,
    max_expanded_bytes: int,
) -> ParseOutput:
    """在超时与安全限制下执行解析；失败统一收敛为稳定业务错误。"""
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(
        target=_parse_child,
        args=(_parser_ref(parser), content, max_expanded_bytes, queue),
    )
    proc.daemon = True
    proc.start()
    try:
        # 先取结果再回收进程：子进程写入大结果时 feeder 会阻塞在管道缓冲上
        # （先 join 后 get 会死锁），get 等待时长即解析时长上限，与 join 语义一致。
        status, value = queue.get(timeout=timeout_seconds)
    except (Empty, EOFError):
        # 超时（或子进程未投递即退出，如被系统杀死）：终止解析子进程，硬性
        # 回收 CPU/内存后收敛 20001（daemon 线程无法被强制终止，会让恶意文件
        # 绕过解析时长资源边界）。
        if proc.is_alive():
            proc.terminate()
        proc.join(_PROCESS_TERMINATE_GRACE_SECONDS)
        raise ParseError(PARSE_FAILED_CODE, PARSE_FAILED_MSG) from None
    proc.join(_PROCESS_TERMINATE_GRACE_SECONDS)
    if status == "error":
        raise value
    result = value
    if not result.normalized_text or not result.normalized_text.strip():
        raise ParseError(EMPTY_DOC_CODE, EMPTY_DOC_MSG)
    return result
