"""上传幂等记录仓储（T047 / FR-031）。

- 幂等作用域为 ``user_id + knowledge_base_id + idempotency_key``；
- 同键不同请求（指纹不同）必须冲突，不得复用首次结果；
- 快照与资料/任务状态在同一事务更新（accepted/failed），不含文件正文或凭证；
- 过期记录由维护扫描器批量删除。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import UploadRequestStatus
from app.models.upload_request import DocumentUploadRequest


class UploadRequestRepository:
    """批量上传幂等记录仓储（租户范围固定当前用户）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find(
        self, user_id: uuid.UUID, knowledge_base_id: uuid.UUID, idempotency_key: str
    ) -> DocumentUploadRequest | None:
        return self.session.scalar(
            select(DocumentUploadRequest).where(
                DocumentUploadRequest.user_id == user_id,
                DocumentUploadRequest.knowledge_base_id == knowledge_base_id,
                DocumentUploadRequest.idempotency_key == idempotency_key,
            )
        )

    def create(
        self,
        *,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        idempotency_key: str,
        request_fingerprint: str,
        upload_batch_id: uuid.UUID,
        expires_at: datetime,
    ) -> DocumentUploadRequest:
        row = DocumentUploadRequest(
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            response_snapshot={},
            status=UploadRequestStatus.COORDINATING,
            upload_batch_id=upload_batch_id,
            expires_at=expires_at,
        )
        self.session.add(row)
        return row

    def update_converged(
        self,
        request: DocumentUploadRequest,
        *,
        status: UploadRequestStatus,
        snapshot: dict[str, Any],
        now: datetime,
    ) -> None:
        request.status = status
        request.response_snapshot = snapshot
        request.updated_at = now

    def delete_expired(self, now: datetime) -> int:
        rows = self.session.scalars(
            select(DocumentUploadRequest).where(DocumentUploadRequest.expires_at < now)
        ).all()
        for row in rows:
            self.session.delete(row)
        return len(rows)
