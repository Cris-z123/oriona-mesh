"""Add the active knowledge-base normalized-name uniqueness invariant.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_knowledge_bases_active_user_normalized_name"


class KnowledgeBaseNormalizedNameMigrationError(RuntimeError):
    """已有数据不满足规范化名称不变量时中止迁移。"""


def _normalize_name(value: str) -> str:
    return value.strip().casefold()


def upgrade() -> None:
    op.add_column(
        "knowledge_bases",
        sa.Column("normalized_name", sa.String(length=360), nullable=True),
    )

    if context.is_offline_mode():
        # --sql 不可读取待迁移数据库；输出的 SQL 对空表可完整执行。已有数据必须在
        # 可执行 Python Unicode casefold 回填与审计的在线模式迁移，不能降级为 lower。
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM knowledge_bases) THEN
                    RAISE EXCEPTION
                        'knowledge_bases contains rows; run migration 0003 online for '
                        'Unicode casefold backfill and conflict audit';
                END IF;
            END $$;
            """
        )
    else:
        _backfill_normalized_names(op.get_bind())

    op.alter_column("knowledge_bases", "normalized_name", nullable=False)
    op.create_check_constraint(
        "ck_knowledge_bases_normalized_name_nonempty",
        "knowledge_bases",
        "normalized_name <> ''",
    )
    op.create_index(
        _INDEX_NAME,
        "knowledge_bases",
        ["user_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="knowledge_bases")
    op.drop_constraint(
        "ck_knowledge_bases_normalized_name_nonempty",
        "knowledge_bases",
        type_="check",
    )
    op.drop_column("knowledge_bases", "normalized_name")


def _backfill_normalized_names(bind: sa.Connection) -> None:
    """审计已有名称后再写入，冲突或空白名称均要求人工修正后重试。"""
    rows = list(
        bind.execute(
            sa.text("SELECT id, user_id, name, status FROM knowledge_bases ORDER BY user_id, id")
        ).mappings()
    )
    values: list[tuple[object, str]] = []
    active_names: dict[tuple[object, str], list[object]] = {}
    blank_names: list[tuple[object, object]] = []

    for row in rows:
        normalized_name = _normalize_name(row["name"])
        values.append((row["id"], normalized_name))
        if not normalized_name:
            blank_names.append((row["user_id"], row["id"]))
        if row["status"] == "active":
            active_names.setdefault((row["user_id"], normalized_name), []).append(row["id"])

    conflicts = [
        (user_id, normalized_name, kb_ids)
        for (user_id, normalized_name), kb_ids in active_names.items()
        if len(kb_ids) > 1
    ]
    if blank_names or conflicts:
        raise KnowledgeBaseNormalizedNameMigrationError(
            _migration_conflict_message(blank_names, conflicts)
        )

    for knowledge_base_id, normalized_name in values:
        bind.execute(
            sa.text("UPDATE knowledge_bases SET normalized_name = :normalized_name WHERE id = :id"),
            {"id": knowledge_base_id, "normalized_name": normalized_name},
        )


def _migration_conflict_message(
    blank_names: list[tuple[object, object]],
    conflicts: list[tuple[object, str, list[object]]],
) -> str:
    """提供可审计的受影响 ID，要求管理员改名后重新执行迁移。"""
    details: list[str] = []
    if blank_names:
        details.append(
            "blank normalized names: "
            + "; ".join(f"user_id={user_id}, kb_id={kb_id}" for user_id, kb_id in blank_names)
        )
    for user_id, normalized_name, kb_ids in conflicts:
        details.append(
            "duplicate active normalized name "
            f"{normalized_name!r}: user_id={user_id}, kb_ids="
            + ",".join(str(kb_id) for kb_id in kb_ids)
        )
    return (
        "migration 0003 aborted before normalized_name backfill; rename the affected "
        "knowledge bases manually and rerun. " + " | ".join(details)
    )
