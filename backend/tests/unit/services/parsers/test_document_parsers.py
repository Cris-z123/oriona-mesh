"""四类解析器与安全包装单元测试（T039 / FR-030、data-model.md 解析边界）。

覆盖：PDF/DOCX/MD/TXT 正常解析、解析器名称/版本；空文本统一 ``20010``；损坏
资料 ``20001``；压缩炸弹/解压大小/条目数限制；解析超时防护；宏、脚本、外链与
路径穿越内容不得执行或被当作 HTML 输出。
"""

import io
import time
import zipfile

import pytest

from app.models.enums import FileType
from app.services.parsers import get_parser
from app.services.parsers.base import ParseError
from app.services.parsers.security import parse_safely

pytestmark = pytest.mark.unit

_EMPTY_DOC_MSG = "资料内容为空，请删除后重新上传"
_PARSE_FAILED_MSG = "资料解析失败，请删除后重新上传"
_CODE_EMPTY = 20010
_CODE_PARSE = 20001


class SlowParser:
    """超时用例解析器：必须为模块级类（parse_safely 在子进程中按类引用重建）。"""

    def parse(self, content: bytes):
        time.sleep(5)
        raise AssertionError("should not complete")


class ExplodingParser:
    """未知异常用例解析器：模块级类，子进程内抛错应映射为 20001。"""

    def parse(self, content: bytes):
        raise ValueError("boom")


# ---------------------------------------------------------------------------
# 正常解析
# ---------------------------------------------------------------------------


def _make_pdf(text: str = "hello pdf world") -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


def _make_docx(texts: list[str] | None = None) -> bytes:
    from docx import Document

    doc = Document()
    for t in texts if texts is not None else ["hello docx"]:
        doc.add_paragraph(t)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _docx_with_extra_entries(docx_bytes: bytes, extras: dict[str, bytes]) -> bytes:
    """向合法 docx 注入额外 zip 成员（路径穿越/宏等安全用例）。"""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        for name, content in extras.items():
            dst.writestr(name, content)
    return out.getvalue()


class TestNormalParsing:
    @pytest.mark.parametrize(
        ("file_type", "content", "expect_in"),
        [
            (FileType.PDF, _make_pdf("pdf content here"), "pdf content here"),
            (FileType.DOCX, _make_docx(["docx content"]), "docx content"),
            (FileType.MD, b"# Title\n\n**bold** body", "Title"),
            (FileType.TXT, "普通文本内容".encode(), "普通文本内容"),
        ],
        ids=["pdf", "docx", "md", "txt"],  # 二进制内容不进入测试 ID（Windows env 限制）
    )
    def test_parse_extracts_text(self, file_type, content, expect_in) -> None:
        parser = get_parser(file_type)
        result = parser.parse(content)
        assert expect_in in result.normalized_text
        assert result.normalized_text.strip()
        assert result.parser_name
        assert result.parser_version

    def test_txt_utf16_and_bom(self) -> None:
        parser = get_parser(FileType.TXT)
        raw = "utf16 内容".encode("utf-16")
        assert "utf16 内容" in parser.parse(raw).normalized_text
        assert "bom 内容" in parser.parse("﻿bom 内容".encode()).normalized_text

    def test_markdown_plain_text_output(self) -> None:
        parser = get_parser(FileType.MD)
        result = parser.parse(b"# H1\n\n- item1\n- item2\n")
        assert "H1" in result.normalized_text
        assert "item1" in result.normalized_text
        # 不应保留 Markdown 语法符号。
        assert "# H1" not in result.normalized_text


# ---------------------------------------------------------------------------
# 空文档与损坏文档
# ---------------------------------------------------------------------------


class TestEmptyAndCorrupt:
    def test_empty_pdf_20010(self) -> None:
        # 扫描件：有页面但无文本层。
        pdf = _make_pdf("")
        with pytest.raises(ParseError) as exc:
            parse_safely(
                get_parser(FileType.PDF), pdf, timeout_seconds=10, max_expanded_bytes=10**9
            )
        assert exc.value.code == _CODE_EMPTY
        assert exc.value.message == _EMPTY_DOC_MSG

    def test_empty_docx_20010(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_safely(
                get_parser(FileType.DOCX),
                _make_docx([]),
                timeout_seconds=10,
                max_expanded_bytes=10**9,
            )
        assert exc.value.code == _CODE_EMPTY

    def test_whitespace_only_markdown_20010(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_safely(
                get_parser(FileType.MD),
                b"   \n\t\n  ",
                timeout_seconds=10,
                max_expanded_bytes=10**9,
            )
        assert exc.value.code == _CODE_EMPTY

    def test_whitespace_only_txt_20010(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_safely(
                get_parser(FileType.TXT),
                b"  \n  ",
                timeout_seconds=10,
                max_expanded_bytes=10**9,
            )
        assert exc.value.code == _CODE_EMPTY

    def test_corrupted_pdf_20001(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_safely(
                get_parser(FileType.PDF),
                b"%PDF-1.4 not a real pdf at all",
                timeout_seconds=10,
                max_expanded_bytes=10**9,
            )
        assert exc.value.code == _CODE_PARSE
        assert exc.value.message == _PARSE_FAILED_MSG

    def test_corrupted_docx_20001(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_safely(
                get_parser(FileType.DOCX),
                b"PK\x03\x04 not a real docx",
                timeout_seconds=10,
                max_expanded_bytes=10**9,
            )
        assert exc.value.code == _CODE_PARSE


# ---------------------------------------------------------------------------
# 压缩炸弹、条目数、路径穿越与超时
# ---------------------------------------------------------------------------


class TestSecurityLimits:
    def test_docx_zip_bomb_rejected_by_expanded_size(self) -> None:
        # 构造解压后远超上限的合法 docx 结构。
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<w:document/>" + "x" * 100_000)
        with pytest.raises(ParseError) as exc:
            parse_safely(
                get_parser(FileType.DOCX),
                buf.getvalue(),
                timeout_seconds=10,
                max_expanded_bytes=10_000,  # 解压后 100KB > 10KB
            )
        assert exc.value.code == _CODE_PARSE

    def test_docx_too_many_entries_rejected(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(5_000):
                zf.writestr(f"word/frag{i}.xml", "<w:r/>")
        with pytest.raises(ParseError) as exc:
            parse_safely(
                get_parser(FileType.DOCX),
                buf.getvalue(),
                timeout_seconds=10,
                max_expanded_bytes=10**9,
            )
        assert exc.value.code == _CODE_PARSE

    def test_docx_path_traversal_member_safe(self) -> None:
        # 包含 ../ 成员名不会写出文件或逃逸；解析正常完成。
        evil = _docx_with_extra_entries(_make_docx(["safe"]), {"../../evil.txt": b"boom"})
        result = parse_safely(
            get_parser(FileType.DOCX),
            evil,
            timeout_seconds=10,
            max_expanded_bytes=10**9,
        )
        assert "safe" in result.normalized_text

    def test_docx_with_vba_macro_not_executed(self) -> None:
        # 含 vbaProject 的 docx 不被执行宏：解析正常完成且只含正文文本。
        evil = _docx_with_extra_entries(
            _make_docx(["macro ok"]), {"word/vbaProject.bin": b"\xd0\xcf\x11\xe0 fake vba"}
        )
        result = parse_safely(
            get_parser(FileType.DOCX),
            evil,
            timeout_seconds=10,
            max_expanded_bytes=10**9,
        )
        assert "macro ok" in result.normalized_text

    def test_markdown_script_and_external_link_not_emitted(self) -> None:
        parser = get_parser(FileType.MD)
        result = parser.parse(b"<script>alert(1)</script>\n\n[link](https://evil.example/x)")
        # HTML 被禁用：脚本标签不作为可执行 HTML 输出。
        assert "<script" not in result.normalized_text
        assert "alert(1)" not in result.normalized_text

    def test_parse_timeout_rejected(self) -> None:
        # 解析在子进程中执行：超时后终止解析进程并收敛 20001（而非线程退出后
        # 解析继续占用 CPU/内存）。
        with pytest.raises(ParseError) as exc:
            parse_safely(
                SlowParser(),  # type: ignore[arg-type]
                b"x",
                timeout_seconds=0.1,
                max_expanded_bytes=10**9,
            )
        assert exc.value.code == _CODE_PARSE

    def test_parser_raises_unknown_error_maps_to_20001(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_safely(
                ExplodingParser(),  # type: ignore[arg-type]
                b"x",
                timeout_seconds=10,
                max_expanded_bytes=10**9,
            )
        assert exc.value.code == _CODE_PARSE
