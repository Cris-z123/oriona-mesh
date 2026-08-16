"""整批上传前置校验（T045 / FR-024、FR-025、FR-033）。

- 任一文件触发不支持格式（``20009/400``）、单文件超 50MB（``20003/400``）或
  数量超 20（``20004/400``）时整批拒绝且不产生任何业务副作用；
- 校验完成才读取文件内容并计算内容哈希（供幂等指纹与完整性校验使用）；
- 固定提示与 openapi.yaml ``UploadValidationErrorEnvelope`` 一致。
"""

import hashlib
import uuid
from dataclasses import dataclass

from fastapi import UploadFile

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import VALIDATION_ERROR_MSG
from app.models.enums import FileType

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
MAX_FILES_PER_BATCH = 20

_CODE_FILE_TOO_LARGE = 20003
_CODE_TOO_MANY_FILES = 20004
_CODE_UNSUPPORTED_TYPE = 20009

_MSG_FILE_TOO_LARGE = "文件超过 50MB 限制"
_MSG_TOO_MANY_FILES = "单次上传最多 20 个文件"
_MSG_UNSUPPORTED_TYPE = "仅支持 PDF、DOCX、MD 和 TXT 文件"

_EXTENSION_TO_TYPE: dict[str, FileType] = {
    "pdf": FileType.PDF,
    "docx": FileType.DOCX,
    "md": FileType.MD,
    "txt": FileType.TXT,
}


@dataclass(frozen=True)
class ValidatedUpload:
    """通过整批前置校验的单个文件（含内容与哈希）。"""

    filename: str
    file_type: FileType
    file_size: int
    content_hash: str
    content: bytes


def validate_upload_batch(files: list[UploadFile]) -> list[ValidatedUpload]:
    """无副作用校验整批文件；任一失败整批拒绝并抛出对应 ``ApiError``。"""
    if not files:
        raise ApiError(10003, VALIDATION_ERROR_MSG, 400)
    if len(files) > MAX_FILES_PER_BATCH:
        raise ApiError(_CODE_TOO_MANY_FILES, _MSG_TOO_MANY_FILES, 400)

    validated: list[ValidatedUpload] = []
    for file in files:
        extension = _extension(file.filename)
        if extension not in _EXTENSION_TO_TYPE:
            raise ApiError(_CODE_UNSUPPORTED_TYPE, _MSG_UNSUPPORTED_TYPE, 400)
        content = _read_content(file)
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise ApiError(_CODE_FILE_TOO_LARGE, _MSG_FILE_TOO_LARGE, 400)
        validated.append(
            ValidatedUpload(
                filename=file.filename or "unnamed",
                file_type=_EXTENSION_TO_TYPE[extension],
                file_size=len(content),
                content_hash=hashlib.sha256(content).hexdigest(),
                content=content,
            )
        )
    return validated


def request_fingerprint(validated: list[ValidatedUpload]) -> str:
    """由文件数量、名称、大小与内容摘要形成的不可逆幂等指纹。

    同键不同请求（任一文件不同）必须冲突，不得复用首次结果（FR-031）。
    """
    parts = [str(len(validated))]
    for item in validated:
        parts.append(f"{item.filename}:{item.file_size}:{item.content_hash}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def new_document_ids(count: int) -> list[uuid.UUID]:
    """为整批资料预分配内部 ID（临时对象键与资料行共用，保证可推导）。"""
    return [uuid.uuid4() for _ in range(count)]


def _extension(filename: str | None) -> str:
    if not filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


_READ_CHUNK_BYTES = 1024 * 1024


def _read_content(file: UploadFile) -> bytes:
    """分块读取上传文件内容；超过 50MB 立即中止并拒绝（内存有界）。

    先复位位置避免大小探测消费流；任何分块读取失败按超限拒绝。
    """
    stream = file.file if file.file is not None else file
    try:
        stream.seek(0)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_SIZE_BYTES:
                raise ApiError(_CODE_FILE_TOO_LARGE, _MSG_FILE_TOO_LARGE, 400)
            chunks.append(chunk)
        return b"".join(chunks)
    except ApiError:
        raise
    except OSError:
        raise ApiError(_CODE_FILE_TOO_LARGE, _MSG_FILE_TOO_LARGE, 400) from None
