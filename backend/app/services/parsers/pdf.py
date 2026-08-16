"""PyMuPDF PDF 解析器（T051 / quickstart 解析边界）。

只提取文本层；不执行任何嵌入脚本/动作（PyMuPDF 打开时不执行 JavaScript、
表单动作或外部引用）。
"""

from importlib.metadata import version

import pymupdf

from app.services.parsers.base import ParseOutput, parse_failed


class PdfParser:
    name = "pymupdf"

    def __init__(self) -> None:
        try:
            self.parser_version = version("pymupdf")
        except Exception:  # noqa: BLE001 - 版本缺失不阻断解析
            self.parser_version = "unknown"

    def parse(self, content: bytes, max_expanded_bytes: int | None = None) -> ParseOutput:
        try:
            document = pymupdf.open(stream=content, filetype="pdf")
        except Exception:
            raise parse_failed() from None
        try:
            pages = [str(page.get_text("text")) for page in document]
            return ParseOutput(
                normalized_text="\n".join(pages),
                parser_name=self.name,
                parser_version=self.parser_version,
                page_count=len(pages),
            )
        except Exception:
            raise parse_failed() from None
        finally:
            document.close()
