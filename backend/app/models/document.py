"""资料实体。

- storage_path 保存与存储后端无关的相对对象键，禁止本地绝对路径；
- upload_batch_id 为内部 UUID（整批上传协调与补偿使用），不对外作为资源 ID；
- content_hash 仅用于完整性校验与诊断，不唯一、不得据此合并重复上传；
- retry_count 镜像当前任务（current_task_type）已启动的重试次数，不做全流水线累计；
- delete_cycle 为删除清理轮次（初始 0，首次删除及 delete_failed 重试时递增）。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.constants import ASYNC_ERROR_CODES
from app.models.enums import DocumentStatus, DocumentTaskType, FileType, enum_values

_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            f"error_code IS NULL OR error_code IN {ASYNC_ERROR_CODES}",
            name="ck_documents_async_error_code",
        ),
        CheckConstraint(
            f"file_size >= 0 AND file_size <= {_MAX_FILE_SIZE_BYTES}",
            name="ck_documents_file_size",
        ),
        CheckConstraint("version >= 1", name="ck_documents_version"),
        CheckConstraint("chunk_count >= 0", name="ck_documents_chunk_count"),
        CheckConstraint("retry_count >= 0", name="ck_documents_retry_count"),
        CheckConstraint("delete_cycle >= 0", name="ck_documents_delete_cycle"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[FileType] = mapped_column(
        Enum(FileType, name="file_type", native_enum=True, values_callable=enum_values),
        nullable=False,
    )
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    upload_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status", native_enum=True, values_callable=enum_values),
        nullable=False,
        default=DocumentStatus.PENDING,
        server_default=text("'pending'"),
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    current_task_type: Mapped[DocumentTaskType | None] = mapped_column(
        Enum(
            DocumentTaskType,
            name="document_task_type",
            native_enum=True,
            values_callable=enum_values,
        )
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    delete_cycle: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(String(500))
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
