"""批量上传幂等记录实体。

- 以 ``user_id + knowledge_base_id + idempotency_key`` 为唯一作用域；
- request_fingerprint 为文件数量、名称、大小与内容摘要形成的不可逆指纹，同键不同
  请求必须冲突，不得复用首次结果；
- response_snapshot 保存首次接受结果的资料 ID 与状态快照；文件全部转正或补偿失败
  时必须与资料/任务状态在同一事务更新为 accepted 或 failed/20011，不得包含文件
  正文或凭证；
- expires_at 幂等保留期（默认 24 小时），过期记录由维护扫描器批量删除。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import UploadRequestStatus, enum_values


class DocumentUploadRequest(Base):
    __tablename__ = "document_upload_requests"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "knowledge_base_id",
            "idempotency_key",
            name="uq_document_upload_requests_scope_key",
        ),
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
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[UploadRequestStatus] = mapped_column(
        Enum(
            UploadRequestStatus,
            name="upload_request_status",
            native_enum=True,
            values_callable=enum_values,
        ),
        nullable=False,
        default=UploadRequestStatus.COORDINATING,
        server_default=text("'coordinating'"),
    )
    upload_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
