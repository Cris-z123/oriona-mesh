"""解析器注册（T051）。

PDF/DOCX/MD/TXT 分别由已锁定的 PyMuPDF、python-docx、markdown-it-py 与
charset-normalizer 处理；解析过程不得发起外部请求、执行宏/脚本或读取对象键
之外的本地路径。
"""

from app.models.enums import FileType
from app.services.parsers.base import DocumentParser
from app.services.parsers.docx import DocxParser
from app.services.parsers.md import MarkdownParser
from app.services.parsers.pdf import PdfParser
from app.services.parsers.txt import TxtParser

_PARSERS: dict[FileType, DocumentParser] = {
    FileType.PDF: PdfParser(),
    FileType.DOCX: DocxParser(),
    FileType.MD: MarkdownParser(),
    FileType.TXT: TxtParser(),
}


def get_parser(file_type: FileType) -> DocumentParser:
    """按文件类型返回解析器实例。"""
    return _PARSERS[file_type]
