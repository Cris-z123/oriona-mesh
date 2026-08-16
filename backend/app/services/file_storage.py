"""资料文件存储服务（T046 / data-model.md 文件与解析结果路径）。

业务层只依赖本服务与 :class:`LocalStorage` 适配器，不拼绝对路径；对象键一律为
相对键且可推导、可检查、可幂等转正、可整批清理。
"""

import uuid
from pathlib import Path

from app.core.settings import get_settings
from app.infrastructure.storage.local import LocalStorage


class FileStorage:
    """本地持久卷的存储门面。"""

    def __init__(self, storage: LocalStorage) -> None:
        self.storage = storage

    @property
    def storage_root(self) -> Path:
        return self.storage.storage_root

    # ------------------------------------------------------------------
    # 批量上传
    # ------------------------------------------------------------------
    def store_batch_temporaries(
        self, batch_id: uuid.UUID, files: list[tuple[uuid.UUID, bytes]]
    ) -> None:
        """把整批内容写入可推导的临时对象键（数据库事务之外）。"""
        for document_id, content in files:
            self.storage.write_temp(batch_id, document_id, content)

    def promote_batch(self, batch_id: uuid.UUID, document_ids: list[uuid.UUID]) -> None:
        for document_id in document_ids:
            self.storage.promote(batch_id, document_id)

    def cleanup_batch(self, batch_id: uuid.UUID) -> None:
        self.storage.cleanup_batch(batch_id)

    def has_temp(self, batch_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        return self.storage.has_temp(batch_id, document_id)

    def has_final(self, batch_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        return self.storage.has_final(batch_id, document_id)

    # ------------------------------------------------------------------
    # 通用对象（解析产物等）
    # ------------------------------------------------------------------
    def write_object(self, object_key: str, content: bytes) -> None:
        self.storage.write_object(object_key, content)

    def read_object(self, object_key: str) -> bytes:
        return self.storage.read_object(object_key)

    def delete_object(self, object_key: str) -> None:
        self.storage.delete_object(object_key)


def default_file_storage() -> FileStorage:
    """按唯一根配置构造默认持久卷适配器。"""
    return FileStorage(LocalStorage(Path(get_settings().storage.storage_root)))
