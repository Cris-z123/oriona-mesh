"""本地持久卷适配器（T046 / data-model.md 本地卷边界）。

- 默认根目录为容器内 ``/data/orionamesh``；数据库只保存相对对象键；
- 对象键由 ``upload_batch_id``/``document_id`` 可推导：临时 ``tmp/{batch}/{doc}``、
  正式 ``obj/{batch}/{doc}``；
- 路径安全：拒绝绝对路径、``..`` 与符号链接逃逸（解析后必须在根目录内）；
- ``promote`` 在同一文件系统上原子重命名，可幂等（正式对象已存在时视为完成）；
- 解析等中间产物使用通用 ``write_object/read_object/delete_object``。
"""

import os
import shutil
import uuid
from pathlib import Path


class LocalStorage:
    """本地持久卷对象存储。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        (self.root / "obj").mkdir(parents=True, exist_ok=True)

    @property
    def storage_root(self) -> Path:
        return self.root

    # ------------------------------------------------------------------
    # 批量上传临时/正式对象
    # ------------------------------------------------------------------
    def write_temp(self, batch_id: uuid.UUID, document_id: uuid.UUID, content: bytes) -> None:
        path = self._temp_path(batch_id, document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def has_temp(self, batch_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        return self._temp_path(batch_id, document_id).is_file()

    def has_final(self, batch_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        return self._final_path(batch_id, document_id).is_file()

    def promote(self, batch_id: uuid.UUID, document_id: uuid.UUID) -> None:
        """同卷原子转正；正式对象已存在时幂等完成。"""
        temp = self._temp_path(batch_id, document_id)
        final = self._final_path(batch_id, document_id)
        if final.is_file():
            return
        if not temp.is_file():
            # 正式与临时对象均缺失：由协调器整批补偿（data-model.md 上传恢复）。
            raise FileNotFoundError(f"upload objects missing for {batch_id}/{document_id}")
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp, final)

    def cleanup_batch(self, batch_id: uuid.UUID) -> None:
        """整批清理临时与正式对象（补偿与删除使用）。"""
        for kind in ("tmp", "obj"):
            shutil.rmtree(self.root / kind / str(batch_id), ignore_errors=True)

    def delete_object(self, object_key: str) -> None:
        """删除单个对象（原始文件/解析产物删除）。"""
        path = self._safe_abs_path(object_key)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def write_object(self, object_key: str, content: bytes) -> None:
        path = self._safe_abs_path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def read_object(self, object_key: str) -> bytes:
        path = self._safe_abs_path(object_key)
        return path.read_bytes()

    # ------------------------------------------------------------------
    # 路径安全
    # ------------------------------------------------------------------
    def _safe_abs_path(self, object_key: str) -> Path:
        """把相对对象键解析为根目录内绝对路径；拒绝逃逸。"""
        if not object_key or Path(object_key).is_absolute() or ".." in Path(object_key).parts:
            raise ValueError(f"unsafe object key: {object_key!r}")
        resolved_root = self.root.resolve()
        candidate = (self.root / object_key).resolve()
        if not candidate.is_relative_to(resolved_root):
            raise ValueError(f"object key escapes storage root: {object_key!r}")
        return candidate

    def _temp_path(self, batch_id: uuid.UUID, document_id: uuid.UUID) -> Path:
        return self.root / "tmp" / str(batch_id) / str(document_id)

    def _final_path(self, batch_id: uuid.UUID, document_id: uuid.UUID) -> Path:
        return self.root / "obj" / str(batch_id) / str(document_id)


def temp_object_key(batch_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """临时对象相对键（上传协调阶段）。"""
    return f"tmp/{batch_id}/{document_id}"


def final_object_key(batch_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """正式对象相对键（资料存储路径）。"""
    return f"obj/{batch_id}/{document_id}"
