"""markdown-it-py Markdown 解析器（T051 / data-model.md 解析边界）。

- 原始 HTML（含 ``<script>`` 等）不作为可执行 HTML 输出：
  从文本中剔除；
- 不解析链接目标、不发起任何外部请求；只提取内联文本内容。
"""

from importlib.metadata import version

from markdown_it import MarkdownIt

from app.services.parsers.base import ParseOutput, parse_failed


class MarkdownParser:
    name = "markdown-it-py"

    def __init__(self) -> None:
        # html: true 使原始 HTML 被识别为 html_inline/html_block 子 token，
        # 随后在文本提取中整体剔除（脚本/外链不得作为内容输出）。
        self._md = MarkdownIt("commonmark", {"html": True}).enable("table")
        try:
            self.parser_version = version("markdown-it-py")
        except Exception:  # noqa: BLE001
            self.parser_version = "unknown"

    def parse(self, content: bytes, max_expanded_bytes: int | None = None) -> ParseOutput:
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError:
            raise parse_failed() from None
        try:
            tokens = self._md.parse(source)
        except Exception:
            raise parse_failed() from None
        parts: list[str] = []
        for token in tokens:
            if token.type in ("html_inline", "html_block"):
                # HTML 被禁用：剔除原始 HTML（脚本/外链不得作为内容输出）。
                continue
            if token.type == "inline":
                # 只保留文本子 token；html_inline 子 token（html=false 时仍存在）
                # 一并剔除，避免脚本标签混入正文。
                for child in token.children or []:
                    if child.type in ("text", "code_inline"):
                        parts.append(child.content)
                    elif child.type in ("softbreak", "hardbreak"):
                        parts.append("\n")
            elif token.type.endswith("_open"):
                parts.append("\n")
        return ParseOutput(
            normalized_text="\n".join(parts),
            parser_name=self.name,
            parser_version=self.parser_version,
        )
