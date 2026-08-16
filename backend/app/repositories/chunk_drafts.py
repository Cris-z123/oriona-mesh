"""草稿片段仓储（T053 / data-model.md 草稿片段边界）。

- 草稿仅供流水线中间阶段使用，不得参与检索；
- 写入必须携带 ``attempt_id`` 并在同一事务通过 fencing 校验；
- 同一版本替换写入（重试安全：先清后写，同事务原子）。
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.chunk import DocumentChunkDraft
from app.repositories.fencing import validate_attempt_write


class ChunkDraftRepository:
    """草稿片段仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_for_version(
        self,
        *,
        attempt_id: uuid.UUID,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
        drafts: list[DocumentChunkDraft],
    ) -> None:
        """attempt_id fencing 事务替换该版本全部草稿。"""
        validate_attempt_write(
            self.session,
            attempt_id=attempt_id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
        )
        self.session.execute(
            delete(DocumentChunkDraft).where(
                DocumentChunkDraft.user_id == user_id,
                DocumentChunkDraft.knowledge_base_id == knowledge_base_id,
                DocumentChunkDraft.document_id == document_id,
                DocumentChunkDraft.document_version == document_version,
            )
        )
        self.session.add_all(drafts)
        self.session.flush()

    def list_for_version(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
    ) -> list[DocumentChunkDraft]:
        return list(
            self.session.scalars(
                select(DocumentChunkDraft)
                .where(
                    DocumentChunkDraft.user_id == user_id,
                    DocumentChunkDraft.knowledge_base_id == knowledge_base_id,
                    DocumentChunkDraft.document_id == document_id,
                    DocumentChunkDraft.document_version == document_version,
                )
                .order_by(DocumentChunkDraft.seq.asc())
            )
        )
