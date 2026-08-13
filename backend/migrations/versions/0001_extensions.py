"""启用 vector、pg_trgm 与 pgcrypto（UUID）扩展

Revision ID: 0001
Revises:
Create Date: 2026-08-13

- ``vector``：pgvector 向量列与余弦相似度检索（Phase 4 双路召回）；
- ``pg_trgm``：关键词相似度检索；
- ``pgcrypto``：提供 ``gen_random_uuid()`` 作为全部 UUID 主键的服务端默认值。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
