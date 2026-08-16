"""解析结果仓储（T051 / data-model.md 解析边界）。

写入必须携带 ``attempt_id`` 并在同一事务通过 fencing 校验；读取固定过滤当前
用户、知识库、资料与精确版本。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chunk import DocumentParseResult
from app.repositories.fencing import validate_attempt_write


class ParseResultRepository:
    """解析结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def save(
        self,
        *,
        attempt_id: uuid.UUID,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
        content_object_key: str,
        content_hash: str,
        parser_name: str,
        parser_version: str,
        normalized_chars: int,
    ) -> DocumentParseResult:
        """attempt_id fencing 事务写入解析结果；校验失败抛 FencingError。"""
        validate_attempt_write(
            self.session,
            attempt_id=attempt_id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
        )
        row = DocumentParseResult(
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            content_object_key=content_object_key,
            content_hash=content_hash,
            parser_name=parser_name,
            parser_version=parser_version,
            normalized_chars=normalized_chars,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def latest_for_version(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        document_id: uuid.UUID,
        document_version: int,
    ) -> DocumentParseResult | None:
        return self.session.scalar(
            select(DocumentParseResult)
            .where(
                DocumentParseResult.user_id == user_id,
                DocumentParseResult.knowledge_base_id == knowledge_base_id,
                DocumentParseResult.document_id == document_id,
                DocumentParseResult.document_version == document_version,
            )
            .order_by(DocumentParseResult.created_at.desc())
            .limit(1)
        )
