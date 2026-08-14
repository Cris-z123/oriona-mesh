"""资料处理任务与任务尝试实体。

- document_tasks 以 ``(id, user_id, knowledge_base_id, document_id, document_version)``
  建立复合唯一键；attempt 通过同序五列复合外键引用父任务，数据库强制四个冗余租户
  边界与父任务完全一致，且创建后不可修改（fencing 的最后一道一致性约束）。
- attempt ID 作为持久化写入的 fencing token；同一任务最多一个未结束（running）attempt
  （部分唯一索引）。
- 初次执行 attempt_no=1/retry_count=0；max_retries 默认 3 表示初次执行外最多重试 3 次，
  单任务最多 4 个 attempt；每个新任务独立计数，与模型网关调用重试相互独立。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.constants import ASYNC_ERROR_CODES
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    enum_values,
)

# 租户边界列：attempt 冗余父任务四列，复合外键按同序引用父任务复合唯一键。
_TENANT_BOUNDARY_COLUMNS = ("user_id", "knowledge_base_id", "document_id", "document_version")


class DocumentTask(Base):
    __tablename__ = "document_tasks"
    __table_args__ = (
        UniqueConstraint(
            "id",
            *_TENANT_BOUNDARY_COLUMNS,
            name="uq_document_tasks_tenant_identity",
        ),
        Index(
            "ix_document_tasks_tenant_scope",
            "user_id",
            "knowledge_base_id",
            "document_id",
            "document_version",
        ),
        CheckConstraint(
            "(task_type = 'delete_cleanup' AND delete_cycle > 0) "
            "OR (task_type <> 'delete_cleanup' AND delete_cycle = 0)",
            name="ck_document_tasks_delete_cycle",
        ),
        CheckConstraint(
            f"error_code IS NULL OR error_code IN {ASYNC_ERROR_CODES}",
            name="ck_document_tasks_async_error_code",
        ),
        CheckConstraint("retry_count >= 0 AND max_retries >= 0", name="ck_document_tasks_retries"),
        CheckConstraint("processed_items >= 0", name="ck_document_tasks_processed_items"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_type: Mapped[DocumentTaskType] = mapped_column(
        Enum(
            DocumentTaskType,
            name="document_task_type",
            native_enum=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    delete_cycle: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[DocumentTaskStatus] = mapped_column(
        Enum(
            DocumentTaskStatus,
            name="document_task_status",
            native_enum=True,
            values_callable=enum_values,
        ),
        nullable=False,
        default=DocumentTaskStatus.PENDING,
        server_default=text("'pending'"),
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    total_items: Mapped[int | None] = mapped_column(Integer)
    processed_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DocumentTaskAttempt(Base):
    __tablename__ = "document_task_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", *_TENANT_BOUNDARY_COLUMNS],
            [
                "document_tasks.id",
                "document_tasks.user_id",
                "document_tasks.knowledge_base_id",
                "document_tasks.document_id",
                "document_tasks.document_version",
            ],
            name="fk_document_task_attempts_tenant_task",
            ondelete="CASCADE",
        ),
        UniqueConstraint("task_id", "attempt_no", name="uq_document_task_attempts_attempt_no"),
        Index(
            "uq_document_task_attempts_open",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "ix_document_task_attempts_tenant_scope",
            "user_id",
            "knowledge_base_id",
            "document_id",
            "document_version",
        ),
        CheckConstraint("attempt_no >= 1", name="ck_document_task_attempts_attempt_no"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_document_task_attempts_duration"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # 冗余父任务租户边界；不可变，由复合外键强制与父任务一致。
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[DocumentAttemptStatus] = mapped_column(
        Enum(
            DocumentAttemptStatus,
            name="document_attempt_status",
            native_enum=True,
            values_callable=enum_values,
        ),
        nullable=False,
        default=DocumentAttemptStatus.RUNNING,
        server_default=text("'running'"),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(String(500))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
