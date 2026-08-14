"""对话、消息与回答引用实体。

- 每个对话必须绑定一个当前用户有权访问的知识库（MVP 无纯聊天）；
- message.user_id 为防御性租户边界，复合外键 ``(conversation_id, user_id)`` 强制与
  对话所有者一致；引用同理通过 ``(message_id, user_id)`` 复合外键强制归属；
- assistant 消息状态与结束原因严格配对（openapi.yaml AssistantMessage）；
- 引用快照（chunk_snapshot）必填，只供历史核验，不可恢复原始资料。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    MessageFinishReason,
    MessageRole,
    MessageStatus,
    enum_values,
)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # 供 messages 的 (conversation_id, user_id) 复合外键引用，数据库强制消息用户边界。
        UniqueConstraint("id", "user_id", name="uq_conversations_identity_user"),
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
    title: Mapped[str | None] = mapped_column(String(200))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            name="fk_messages_conversation_user",
            ondelete="CASCADE",
        ),
        # 供 message_citations 的 (message_id, user_id) 复合外键引用。
        UniqueConstraint("id", "user_id", name="uq_messages_identity_user"),
        Index(
            "ix_messages_user_conversation_created",
            "user_id",
            "conversation_id",
            "created_at",
        ),
        CheckConstraint(
            "(role = 'user' AND status = 'completed' AND finish_reason IS NULL) "
            "OR (role = 'assistant' AND status = 'streaming' AND finish_reason IS NULL) "
            "OR (role = 'assistant' AND status = 'completed' "
            "AND finish_reason IN ('stop', 'length')) "
            "OR (role = 'assistant' AND status = 'failed' AND finish_reason = 'error') "
            "OR (role = 'assistant' AND status = 'cancelled' AND finish_reason = 'cancelled')",
            name="ck_messages_status_finish_reason",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role", native_enum=True, values_callable=enum_values),
        nullable=False,
    )
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status", native_enum=True, values_callable=enum_values),
        nullable=False,
    )
    finish_reason: Mapped[MessageFinishReason | None] = mapped_column(
        Enum(
            MessageFinishReason,
            name="message_finish_reason",
            native_enum=True,
            values_callable=enum_values,
        )
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MessageCitation(Base):
    __tablename__ = "message_citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["message_id", "user_id"],
            ["messages.id", "messages.user_id"],
            name="fk_message_citations_message_user",
            ondelete="CASCADE",
        ),
        # 同一消息内 rank 唯一且 >= 1；数组按 rank 升序返回。
        UniqueConstraint("message_id", "rank", name="uq_message_citations_message_rank"),
        Index("ix_message_citations_user_message", "user_id", "message_id"),
        Index("ix_message_citations_user_kb_rank", "user_id", "knowledge_base_id", "rank"),
        CheckConstraint("rank >= 1", name="ck_message_citations_rank"),
        CheckConstraint("document_version >= 1", name="ck_message_citations_document_version"),
        CheckConstraint("score IS NOT NULL", name="ck_message_citations_score"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 来源当前可访问时必填；资料删除或不可访问时置空并回退快照。
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL")
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    chunk_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
