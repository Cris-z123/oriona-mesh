"""安全解析 runner 的模块入口。

该模块只由 ``python -m app.services.parsers.runner`` 运行；stdin/stdout 是父进程
和 runner 的受限协议，解析器或第三方库不得污染 stdout。stderr 在父进程侧丢弃，
避免解析异常或资料内容进入 worker 日志。
"""

import contextlib
import json
import sys
from typing import Any

from app.services.parsers.base import (
    EMPTY_DOC_CODE,
    EMPTY_DOC_MSG,
    ParseError,
    ParseOutput,
    parse_failed,
)
from app.services.parsers.security import _load_parser


def _write(metadata: dict[str, Any], content: bytes = b"") -> None:
    header = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(header + b"\n" + content)
    sys.stdout.buffer.flush()


def _write_error(exc: ParseError) -> None:
    if exc.code == EMPTY_DOC_CODE:
        _write({"status": "error", "code": EMPTY_DOC_CODE, "message": EMPTY_DOC_MSG})
        return
    failure = parse_failed()
    _write({"status": "error", "code": failure.code, "message": failure.message})


def _read_request() -> tuple[str, int, bytes]:
    header = sys.stdin.buffer.readline()
    if not header or not header.endswith(b"\n"):
        raise ValueError("missing request header")
    metadata = json.loads(header)
    if not isinstance(metadata, dict):
        raise ValueError("invalid request metadata")
    parser_ref = metadata.get("parser_ref")
    max_expanded_bytes = metadata.get("max_expanded_bytes")
    if (
        not isinstance(parser_ref, str)
        or not parser_ref
        or not isinstance(max_expanded_bytes, int)
        or max_expanded_bytes <= 0
    ):
        raise ValueError("invalid request fields")
    return parser_ref, max_expanded_bytes, sys.stdin.buffer.read()


def main() -> int:
    try:
        parser_ref, max_expanded_bytes, content = _read_request()
        parser = _load_parser(parser_ref)
        # stdout 是协议通道；第三方解析器误写 stdout 时，转至已被父进程丢弃的 stderr。
        with contextlib.redirect_stdout(sys.stderr):
            result = parser.parse(content, max_expanded_bytes=max_expanded_bytes)
        if not isinstance(result, ParseOutput):
            raise TypeError("parser returned an invalid result")
    except ParseError as exc:
        _write_error(exc)
        return 0
    except Exception:
        _write_error(parse_failed())
        return 0

    normalized_bytes = result.normalized_text.encode("utf-8")
    if len(normalized_bytes) > max_expanded_bytes:
        _write_error(parse_failed())
        return 0
    _write(
        {
            "status": "result",
            "parser_name": result.parser_name,
            "parser_version": result.parser_version,
            "content_bytes": len(normalized_bytes),
            "page_count": result.page_count,
        },
        normalized_bytes,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
