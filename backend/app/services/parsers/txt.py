"""charset-normalizer 纯文本解析器（T051 / data-model.md 解析边界）。

- 自动检测编码（UTF-8/UTF-16 等）；检测失败映射 ``20001``；
- 去除 UTF-8 BOM；不读取任何本地路径或发起外部请求。
"""

from importlib.metadata import version

from charset_normalizer import from_bytes

from app.services.parsers.base import ParseOutput, parse_failed


class TxtParser:
    name = "charset-normalizer"

    def __init__(self) -> None:
        try:
            self.parser_version = version("charset-normalizer")
        except Exception:  # noqa: BLE001
            self.parser_version = "unknown"

    def parse(self, content: bytes, max_expanded_bytes: int | None = None) -> ParseOutput:
        try:
            best = from_bytes(content).best()
        except Exception:
            raise parse_failed() from None
        if best is None:
            raise parse_failed()
        text = str(best)
        if text.startswith("﻿"):
            text = text[1:]
        return ParseOutput(
            normalized_text=text,
            parser_name=self.name,
            parser_version=self.parser_version,
        )
