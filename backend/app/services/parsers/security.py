"""统一安全解析包装（T051 / data-model.md 解析边界）。

- 解析超时限制：超时后收敛为 ``20001``（线程内解析，不阻塞 worker）；
- 空/纯空白标准化文本收敛为 ``20010 EMPTY_DOCUMENT``，不得进入分块阶段；
- 解析器未知异常统一映射 ``20001`` 固定安全提示，不泄漏内部细节。
"""

import threading
from typing import Any

from app.services.parsers.base import (
    EMPTY_DOC_CODE,
    EMPTY_DOC_MSG,
    PARSE_FAILED_MSG,
    ParseError,
    ParseOutput,
    parse_failed,
)


def parse_safely(
    parser: Any,
    content: bytes,
    *,
    timeout_seconds: float,
    max_expanded_bytes: int,
) -> ParseOutput:
    """在超时与安全限制下执行解析；失败统一收敛为稳定业务错误。"""
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["result"] = parser.parse(content, max_expanded_bytes=max_expanded_bytes)
        except ParseError as exc:
            outcome["error"] = exc
        except Exception:
            outcome["error"] = parse_failed()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise ParseError(20001, PARSE_FAILED_MSG)
    if "error" in outcome:
        raise outcome["error"]
    result = outcome["result"]
    if not result.normalized_text or not result.normalized_text.strip():
        raise ParseError(EMPTY_DOC_CODE, EMPTY_DOC_MSG)
    return result
