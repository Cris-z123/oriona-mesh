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
    Index,
    Integer,
    String,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import KnowledgeBaseStatus, enum_values

# 异步删除清理失败稳定错误码；与 delete_failed 状态配对（T015）。
DELETE_CLEANUP_ERROR_CODE = 20015


def normalize_knowledge_base_name(name: str) -> str:
    """返回知识库名称的内部唯一键：Unicode trim 后 Unicode casefold。"""
    return name.strip().casefold()


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint(
            "(status = 'delete_failed' AND delete_error_code = 20015) "
            "OR (status <> 'delete_failed' AND delete_error_code IS NULL)",
            name="ck_knowledge_bases_delete_error_code",
        ),
        CheckConstraint(
            "normalized_name <> ''",
            name="ck_knowledge_bases_normalized_name_nonempty",
        ),
        Index(
            "uq_knowledge_bases_active_user_normalized_name",
            "user_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("status = 'active'"),
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
    # casefold 可能将单个 Unicode 码位展开为最多三个码位；显示名称上限 120，
    # 因而内部键留足 360 字符，避免把合法名称变成数据库截断错误。
    normalized_name: Mapped[str] = mapped_column(String(360), nullable=False)
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


@event.listens_for(KnowledgeBase, "before_insert")
@event.listens_for(KnowledgeBase, "before_update")
def _write_normalized_name(_mapper, _connection, target: KnowledgeBase) -> None:
    """保证通过 ORM 的内部写入也不会绕过规范化名称不变量。"""
    target.normalized_name = normalize_knowledge_base_name(target.name)
