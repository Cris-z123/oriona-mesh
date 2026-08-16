"""资料级处理并发名额仓储（T052 / FR-032、data-model.md 处理并发名额）。

- 名额归属于整份资料流水线；task_id 仅记录当前阶段归属，随阶段切换更新，
  不得触发释放再获取；
- 获取前在数据库事务内按 ``user_id`` 锁定未释放名额并统计（数据库真相源，
  非进程内计数）；每个资料最多一个未释放名额（部分唯一索引兜底）；
- 心跳续租；资料进入 ``deleting/deleted`` 后不得续租（删除事务冻结等待上限）；
- 终态释放记录原因；释放后不可再次激活。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.enums import DocumentStatus
from app.models.processing_lease import DocumentProcessingLease


class ProcessingLeaseRepository:
    """处理名额租约仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def acquire(
        self,
        *,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        task_id: uuid.UUID,
        lease_seconds: int,
        max_per_user: int,
        now: datetime | None = None,
    ) -> DocumentProcessingLease | None:
        """原子获取资料级名额；超出用户上限或已有未释放名额时返回 None。

        调用方负责提交或回滚；None 时任务保持 queued 由扫描器/重投重试。
        """
        now = now or datetime.now(UTC)
        # 按用户串行化名额分配：对空租约集 FOR UPDATE 无行可锁，并发 worker
        # 会同时通过计数检查；advisory 事务锁保证同用户获取互斥（跨实例一致）。
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"processing-slot:{user_id}"},
        )
        open_leases = self.session.scalars(
            select(DocumentProcessingLease)
            .where(
                DocumentProcessingLease.user_id == user_id,
                DocumentProcessingLease.released_at.is_(None),
            )
            .with_for_update()
        ).all()
        if len(open_leases) >= max_per_user:
            return None
        lease = DocumentProcessingLease(
            user_id=user_id,
            document_id=document_id,
            task_id=task_id,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=lease_seconds),
        )
        self.session.add(lease)
        try:
            self.session.flush()
        except IntegrityError:
            # 同资料已有未释放名额（部分唯一索引）：并发竞争败出。
            self.session.rollback()
            return None
        return lease

    def heartbeat(
        self,
        lease_id: uuid.UUID,
        document_id: uuid.UUID,
        task_id: uuid.UUID,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """续租；租约不存在/已释放或资料 deleting/deleted 时返回 False。"""
        now = now or datetime.now(UTC)
        lease = self.session.scalar(
            select(DocumentProcessingLease)
            .where(DocumentProcessingLease.id == lease_id)
            .with_for_update()
        )
        if lease is None or lease.released_at is not None:
            return False
        document = self.session.scalar(
            select(Document).where(Document.id == document_id).with_for_update()
        )
        if document is None or document.status in (DocumentStatus.DELETING, DocumentStatus.DELETED):
            # 删除提交后不得续租：expires_at 保持删除事务冻结的上限。
            return False
        lease.heartbeat_at = now
        lease.expires_at = now + timedelta(seconds=lease_seconds)
        lease.task_id = task_id
        return True

    def release(
        self,
        lease_id: uuid.UUID,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        lease = self.session.get(DocumentProcessingLease, lease_id)
        if lease is not None and lease.released_at is None:
            lease.released_at = now
            lease.release_reason = reason

    def find_open(self, document_id: uuid.UUID) -> DocumentProcessingLease | None:
        return self.session.scalar(
            select(DocumentProcessingLease).where(
                DocumentProcessingLease.document_id == document_id,
                DocumentProcessingLease.released_at.is_(None),
            )
        )

    def lock_open(self, document_id: uuid.UUID) -> DocumentProcessingLease | None:
        """锁定资料的未释放租约（删除事务冻结等待上限使用）。"""
        return self.session.scalar(
            select(DocumentProcessingLease)
            .where(
                DocumentProcessingLease.document_id == document_id,
                DocumentProcessingLease.released_at.is_(None),
            )
            .with_for_update()
        )

    def list_expired_open(self, now: datetime) -> list[DocumentProcessingLease]:
        return list(
            self.session.scalars(
                select(DocumentProcessingLease).where(
                    DocumentProcessingLease.released_at.is_(None),
                    DocumentProcessingLease.expires_at < now,
                )
            )
        )
