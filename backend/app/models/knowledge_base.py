"""知识库实体。

- 所有访问以 user_id 为边界；deleting 从列表、详情、对话创建和检索中隐藏；
- delete_failed 仅向所属用户返回最小“删除未完成”墓碑与 retry_delete；
- delete_error_code 仅在 status=delete_failed 时固定为 20015（数据库配对约束）。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
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
from app.models.enums import KnowledgeBaseStatus, enum_values

# 异步删除清理失败稳定错误码；与 delete_failed 状态配对（T015）。
DELETE_CLEANUP_ERROR_CODE = 20015


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint(
            "(status = 'delete_failed' AND delete_error_code = 20015) "
            "OR (status <> 'delete_failed' AND delete_error_code IS NULL)",
            name="ck_knowledge_bases_delete_error_code",
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
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[KnowledgeBaseStatus] = mapped_column(
        Enum(
            KnowledgeBaseStatus,
            name="knowledge_base_status",
            native_enum=True,
            values_callable=enum_values,
        ),
        nullable=False,
        default=KnowledgeBaseStatus.ACTIVE,
        server_default=text("'active'"),
    )
    delete_error_code: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
