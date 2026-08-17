"""解析器端口与统一输出（T051 / data-model.md 解析边界）。"""

from dataclasses import dataclass
from typing import Protocol

PARSE_FAILED_CODE = 20001
PARSE_FAILED_MSG = "资料解析失败，请删除后重新上传"
EMPTY_DOC_CODE = 20010
EMPTY_DOC_MSG = "资料内容为空，请删除后重新上传"


@dataclass(frozen=True)
class ParseOutput:
    """解析结果（标准化文本与解析器元数据）。"""

    normalized_text: str
    parser_name: str
    parser_version: str
    page_count: int | None = None


class ParseError(Exception):
    """解析失败：稳定业务错误码 + 固定安全提示。"""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def __reduce__(self) -> tuple:
        # Exception.__reduce__ 用 self.args 重建会丢失 code（args 只有 message）；
        # 显式携带双参，保证解析异常能跨进程（子进程解析）重建。
        return (type(self), (self.code, self.message))


class DocumentParser(Protocol):
    """解析器端口；``max_expanded_bytes`` 由安全包装按配置传入。"""

    def parse(self, content: bytes, max_expanded_bytes: int | None = None) -> ParseOutput: ...


def parse_failed() -> ParseError:
    return ParseError(PARSE_FAILED_CODE, PARSE_FAILED_MSG)
