"""统一安全解析包装（T051 / data-model.md 解析边界）。

解析器在独立的操作系统子进程中运行。Celery prefork worker 本身是 daemon
进程，不能再用 ``multiprocessing.Process`` 创建子进程；此处改用
``subprocess.Popen`` 启动无状态 runner，保留解析时长的硬边界而不降低 worker
并发模型。

父进程和 runner 仅交换一行 JSON 元数据及原始字节：不通过 pickle 传递 Python
对象、异常或解析器状态。任何启动、通信或协议异常都收敛为稳定 ``20001``。
"""

import importlib
import json
import subprocess
import sys
from typing import Any

from app.services.parsers.base import (
    EMPTY_DOC_CODE,
    EMPTY_DOC_MSG,
    ParseError,
    ParseOutput,
    parse_failed,
)

# 子进程收到 kill 后等待回收的上限；正常解析的时长由调用方配置控制。
_PROCESS_TERMINATE_GRACE_SECONDS = 10.0


def _parser_ref(parser: Any) -> str:
    """把解析器实例编码为可在 runner 内零参重建的模块级类引用。"""
    cls = type(parser)
    if cls.__module__ == "__main__" or cls.__qualname__ != cls.__name__:
        raise ValueError(f"parser must be a module-level class: {cls!r}")
    return f"{cls.__module__}.{cls.__name__}"


def _load_parser(ref: str) -> Any:
    """按模块级类引用重建解析器；仅供隔离 runner 调用。"""
    module_name, _, class_name = ref.rpartition(".")
    if not module_name or not class_name:
        raise ValueError("invalid parser reference")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


def _encode_request(parser_ref: str, content: bytes, max_expanded_bytes: int) -> bytes:
    metadata = json.dumps(
        {"parser_ref": parser_ref, "max_expanded_bytes": max_expanded_bytes},
        separators=(",", ":"),
    ).encode("utf-8")
    return metadata + b"\n" + content


def _parse_response(payload: bytes) -> ParseOutput:
    header, separator, normalized_bytes = payload.partition(b"\n")
    if not separator:
        raise parse_failed()
    try:
        metadata = json.loads(header)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise parse_failed() from None
    if not isinstance(metadata, dict):
        raise parse_failed()

    status = metadata.get("status")
    if status == "error":
        if metadata.get("code") == EMPTY_DOC_CODE:
            raise ParseError(EMPTY_DOC_CODE, EMPTY_DOC_MSG)
        raise parse_failed()
    if status != "result":
        raise parse_failed()

    parser_name = metadata.get("parser_name")
    parser_version = metadata.get("parser_version")
    content_bytes = metadata.get("content_bytes")
    page_count = metadata.get("page_count")
    if (
        not isinstance(parser_name, str)
        or not parser_name
        or not isinstance(parser_version, str)
        or not parser_version
        or not isinstance(content_bytes, int)
        or content_bytes < 0
        or content_bytes != len(normalized_bytes)
        or (page_count is not None and (not isinstance(page_count, int) or page_count < 0))
    ):
        raise parse_failed()
    try:
        normalized_text = normalized_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise parse_failed() from None
    return ParseOutput(
        normalized_text=normalized_text,
        parser_name=parser_name,
        parser_version=parser_version,
        page_count=page_count,
    )


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """强制终止超时 runner，并在有限时间内回收其进程句柄。"""
    if proc.poll() is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        proc.communicate(timeout=_PROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # kill 后仍不能回收属于运行环境异常；对调用方维持相同稳定错误。
        raise parse_failed() from None


def parse_safely(
    parser: Any,
    content: bytes,
    *,
    timeout_seconds: float,
    max_expanded_bytes: int,
) -> ParseOutput:
    """在独立 runner 中安全解析，超时和内部异常收敛为稳定业务错误。"""
    try:
        request = _encode_request(_parser_ref(parser), content, max_expanded_bytes)
    except (TypeError, ValueError):
        raise parse_failed() from None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.services.parsers.runner"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        raise parse_failed() from None

    try:
        response, _ = proc.communicate(request, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_and_reap(proc)
        raise parse_failed() from None

    if proc.returncode != 0:
        raise parse_failed()
    result = _parse_response(response)
    if not result.normalized_text or not result.normalized_text.strip():
        raise ParseError(EMPTY_DOC_CODE, EMPTY_DOC_MSG)
    return result
