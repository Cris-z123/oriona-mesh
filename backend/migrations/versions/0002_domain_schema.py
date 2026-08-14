"""领域实体：身份、租户边界、资料处理、检索与对话

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-14

本迁移建立全部领域表（data-model.md 的物理实现），包括：

- 实体关系与独立状态枚举（知识库/资料/任务/尝试/消息各有独立枚举）；
- 租户边界：attempt 通过五列复合外键冗余父任务边界；消息/引用通过复合外键
  强制与对话/消息所有者一致；
- 资料/任务异步 error_code 约束（20001/20010~20015/50000）、知识库
  delete_failed 与 delete_error_code=20015 配对约束、资料 upload_batch_id
  索引、上传幂等唯一键、资料级处理名额部分唯一索引、同一任务最多一个
  running attempt 部分唯一索引、delete_cleanup/delete_cycle 约束、
  users.email 规范化后唯一、assistant 状态/结束原因配对及 Citation
  非空/排名唯一约束。

枚举类型显式创建（create_type=False），避免多表共用同一枚举时重复 CREATE TYPE。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID_PK = sa.Uuid(as_uuid=True)
_SERVER_UUID = sa.text("gen_random_uuid()")
_SERVER_NOW = sa.text("now()")


def _enum(name: str, *values: str) -> postgresql.ENUM:
    """声明引用既有枚举类型的列类型；类型由 upgrade 顶部显式创建。"""
    return postgresql.ENUM(*values, name=name, create_type=False)


_ENUMS = (
    ("knowledge_base_status", "active", "deleting", "delete_failed"),
    ("file_type", "pdf", "docx", "md", "txt"),
    (
        "document_status",
        "pending",
        "queued",
        "processing",
        "completed",
        "failed",
        "deleting",
        "deleted",
    ),
    ("document_task_type", "parse", "chunk", "embed", "finalize", "cleanup", "delete_cleanup"),
    ("document_task_status", "pending", "queued", "running", "succeeded", "failed", "cancelled"),
    ("document_attempt_status", "running", "succeeded", "failed", "cancelled"),
    ("upload_request_status", "coordinating", "accepted", "failed"),
    ("message_role", "user", "assistant"),
    ("message_status", "streaming", "completed", "failed", "cancelled"),
    ("message_finish_reason", "stop", "length", "error", "cancelled"),
)


def upgrade() -> None:
    for name, *values in _ENUMS:
        quoted = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({quoted})")

    # --- users / auth_sessions ---
    op.create_table(
        "users",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("refresh_token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("rotated_from_session_id", _UUID_PK),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
    )

    # --- knowledge_bases ---
    op.create_table(
        "knowledge_bases",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column(
            "status", _enum("knowledge_base_status"), nullable=False, server_default="active"
        ),
        sa.Column("delete_error_code", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.CheckConstraint(
            "(status = 'delete_failed' AND delete_error_code = 20015) "
            "OR (status <> 'delete_failed' AND delete_error_code IS NULL)",
            name="ck_knowledge_bases_delete_error_code",
        ),
    )

    # --- conversations（先于 documents，消息复合外键依赖其唯一键）---
    op.create_table(
        "conversations",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "knowledge_base_id",
            _UUID_PK,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_conversations_identity_user"),
    )

    # --- 上传幂等 ---
    op.create_table(
        "document_upload_requests",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "knowledge_base_id",
            _UUID_PK,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("response_snapshot", postgresql.JSONB, nullable=False),
        sa.Column(
            "status", _enum("upload_request_status"), nullable=False, server_default="coordinating"
        ),
        sa.Column("upload_batch_id", _UUID_PK, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.UniqueConstraint(
            "user_id",
            "knowledge_base_id",
            "idempotency_key",
            name="uq_document_upload_requests_scope_key",
        ),
    )

    # --- documents ---
    op.create_table(
        "documents",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "knowledge_base_id",
            _UUID_PK,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_type", _enum("file_type"), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("upload_batch_id", _UUID_PK, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", _enum("document_status"), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("current_task_type", _enum("document_task_type")),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("delete_cycle", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error_code", sa.Integer),
        sa.Column("error_message", sa.String(500)),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("processing_started_at", sa.DateTime(timezone=True)),
        sa.Column("processing_finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN "
            "(20001, 20010, 20011, 20012, 20013, 20014, 20015, 50000)",
            name="ck_documents_async_error_code",
        ),
        sa.CheckConstraint(
            "file_size >= 0 AND file_size <= 52428800", name="ck_documents_file_size"
        ),
        sa.CheckConstraint("version >= 1", name="ck_documents_version"),
        sa.CheckConstraint("chunk_count >= 0", name="ck_documents_chunk_count"),
        sa.CheckConstraint("retry_count >= 0", name="ck_documents_retry_count"),
        sa.CheckConstraint("delete_cycle >= 0", name="ck_documents_delete_cycle"),
    )

    # --- 解析/草稿/正式片段（chunks 依赖 documents）---
    op.create_table(
        "document_parse_results",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "knowledge_base_id",
            _UUID_PK,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            _UUID_PK,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_version", sa.Integer, nullable=False),
        sa.Column("content_object_key", sa.String(500), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parser_name", sa.String(120), nullable=False),
        sa.Column("parser_version", sa.String(40), nullable=False),
        sa.Column("normalized_chars", sa.Integer, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
    )
    op.create_table(
        "document_chunk_drafts",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "knowledge_base_id",
            _UUID_PK,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            _UUID_PK,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_version", sa.Integer, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("page", sa.Integer),
        sa.Column("section", sa.String(200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.UniqueConstraint(
            "document_id", "document_version", "seq", name="uq_document_chunk_drafts_seq"
        ),
    )
    op.create_table(
        "chunks",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "knowledge_base_id",
            _UUID_PK,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            _UUID_PK,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_version", sa.Integer, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("embedding_model", sa.String(120), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("page", sa.Integer),
        sa.Column("section", sa.String(200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.UniqueConstraint(
            "document_id",
            "document_version",
            "policy_version",
            "embedding_model",
            "seq",
            name="uq_chunks_logic_key",
        ),
    )

    # --- 处理名额 ---
    op.create_table(
        "document_processing_leases",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "document_id",
            _UUID_PK,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", _UUID_PK),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("release_reason", sa.String(120)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.Index(
            "uq_document_processing_leases_open",
            "document_id",
            unique=True,
            postgresql_where=sa.text("released_at IS NULL"),
        ),
    )

    # --- 任务与尝试（attempt 五列复合外键）---
    op.create_table(
        "document_tasks",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column(
            "user_id", _UUID_PK, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("knowledge_base_id", _UUID_PK, nullable=False),
        sa.Column(
            "document_id",
            _UUID_PK,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_version", sa.Integer, nullable=False),
        sa.Column("task_type", _enum("document_task_type"), nullable=False),
        sa.Column("delete_cycle", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "status", _enum("document_task_status"), nullable=False, server_default="pending"
        ),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default=sa.text("3")),
        sa.Column("total_items", sa.Integer),
        sa.Column("processed_items", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error_code", sa.Integer),
        sa.Column("error_message", sa.String(500)),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.UniqueConstraint(
            "id",
            "user_id",
            "knowledge_base_id",
            "document_id",
            "document_version",
            name="uq_document_tasks_tenant_identity",
        ),
        sa.CheckConstraint(
            "(task_type = 'delete_cleanup' AND delete_cycle > 0) "
            "OR (task_type <> 'delete_cleanup' AND delete_cycle = 0)",
            name="ck_document_tasks_delete_cycle",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN "
            "(20001, 20010, 20011, 20012, 20013, 20014, 20015, 50000)",
            name="ck_document_tasks_async_error_code",
        ),
        sa.CheckConstraint(
            "retry_count >= 0 AND max_retries >= 0", name="ck_document_tasks_retries"
        ),
        sa.CheckConstraint("processed_items >= 0", name="ck_document_tasks_processed_items"),
    )
    op.create_table(
        "document_task_attempts",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column("task_id", _UUID_PK, nullable=False),
        sa.Column("user_id", _UUID_PK, nullable=False),
        sa.Column("knowledge_base_id", _UUID_PK, nullable=False),
        sa.Column("document_id", _UUID_PK, nullable=False),
        sa.Column("document_version", sa.Integer, nullable=False),
        sa.Column("attempt_no", sa.Integer, nullable=False),
        sa.Column("worker_name", sa.String(120)),
        sa.Column(
            "status", _enum("document_attempt_status"), nullable=False, server_default="running"
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "user_id", "knowledge_base_id", "document_id", "document_version"],
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
        sa.UniqueConstraint("task_id", "attempt_no", name="uq_document_task_attempts_attempt_no"),
        sa.Index(
            "uq_document_task_attempts_open",
            "task_id",
            unique=True,
            postgresql_where=sa.text("status = 'running'"),
        ),
        sa.CheckConstraint("attempt_no >= 1", name="ck_document_task_attempts_attempt_no"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_document_task_attempts_duration"
        ),
    )

    # --- messages / message_citations ---
    op.create_table(
        "messages",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column("user_id", _UUID_PK, nullable=False),
        sa.Column("conversation_id", _UUID_PK, nullable=False),
        sa.Column("role", _enum("message_role"), nullable=False),
        sa.Column("status", _enum("message_status"), nullable=False),
        sa.Column("finish_reason", _enum("message_finish_reason")),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("rewritten_query", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["conversations.id", "conversations.user_id"],
            name="fk_messages_conversation_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_messages_identity_user"),
        sa.CheckConstraint(
            "(role = 'user' AND status = 'completed' AND finish_reason IS NULL) "
            "OR (role = 'assistant' AND status = 'streaming' AND finish_reason IS NULL) "
            "OR (role = 'assistant' AND status = 'completed' "
            "AND finish_reason IN ('stop', 'length')) "
            "OR (role = 'assistant' AND status = 'failed' AND finish_reason = 'error') "
            "OR (role = 'assistant' AND status = 'cancelled' AND finish_reason = 'cancelled')",
            name="ck_messages_status_finish_reason",
        ),
    )
    op.create_table(
        "message_citations",
        sa.Column("id", _UUID_PK, primary_key=True, server_default=_SERVER_UUID),
        sa.Column("message_id", _UUID_PK, nullable=False),
        sa.Column("user_id", _UUID_PK, nullable=False),
        sa.Column(
            "knowledge_base_id",
            _UUID_PK,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_id", _UUID_PK, sa.ForeignKey("chunks.id", ondelete="SET NULL")),
        sa.Column("document_id", _UUID_PK, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("document_version", sa.Integer, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("chunk_snapshot", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=_SERVER_NOW
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "user_id"],
            ["messages.id", "messages.user_id"],
            name="fk_message_citations_message_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("message_id", "rank", name="uq_message_citations_message_rank"),
        sa.CheckConstraint("rank >= 1", name="ck_message_citations_rank"),
        sa.CheckConstraint("document_version >= 1", name="ck_message_citations_document_version"),
        sa.CheckConstraint("score IS NOT NULL", name="ck_message_citations_score"),
    )

    # --- 诊断与租户索引 ---
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_knowledge_bases_user_id", "knowledge_bases", ["user_id"])
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_knowledge_base_id", "conversations", ["knowledge_base_id"])
    op.create_index("ix_document_upload_requests_user_id", "document_upload_requests", ["user_id"])
    op.create_index(
        "ix_document_upload_requests_knowledge_base_id",
        "document_upload_requests",
        ["knowledge_base_id"],
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"])
    op.create_index("ix_documents_knowledge_base_id", "documents", ["knowledge_base_id"])
    op.create_index("ix_documents_upload_batch_id", "documents", ["upload_batch_id"])
    op.create_index("ix_document_parse_results_user_id", "document_parse_results", ["user_id"])
    op.create_index(
        "ix_document_parse_results_document_version",
        "document_parse_results",
        ["document_id", "document_version"],
    )
    op.create_index("ix_document_chunk_drafts_user_id", "document_chunk_drafts", ["user_id"])
    op.create_index(
        "ix_document_chunk_drafts_document_version",
        "document_chunk_drafts",
        ["document_id", "document_version"],
    )
    op.create_index("ix_chunks_user_id", "chunks", ["user_id"])
    op.create_index("ix_chunks_document_version", "chunks", ["document_id", "document_version"])
    op.create_index(
        "ix_document_processing_leases_user_id", "document_processing_leases", ["user_id"]
    )
    op.create_index(
        "ix_document_tasks_tenant_scope",
        "document_tasks",
        ["user_id", "knowledge_base_id", "document_id", "document_version"],
    )
    op.create_index(
        "ix_document_task_attempts_tenant_scope",
        "document_task_attempts",
        ["user_id", "knowledge_base_id", "document_id", "document_version"],
    )
    op.create_index(
        "ix_messages_user_conversation_created",
        "messages",
        ["user_id", "conversation_id", "created_at"],
    )
    op.create_index(
        "ix_message_citations_user_message", "message_citations", ["user_id", "message_id"]
    )
    op.create_index(
        "ix_message_citations_user_kb_rank",
        "message_citations",
        ["user_id", "knowledge_base_id", "rank"],
    )


def downgrade() -> None:
    for table in (
        "message_citations",
        "messages",
        "document_task_attempts",
        "document_tasks",
        "document_processing_leases",
        "chunks",
        "document_chunk_drafts",
        "document_parse_results",
        "documents",
        "document_upload_requests",
        "conversations",
        "knowledge_bases",
        "auth_sessions",
        "users",
    ):
        op.drop_table(table)

    for name, *_ in reversed(_ENUMS):
        op.execute(f"DROP TYPE {name}")
