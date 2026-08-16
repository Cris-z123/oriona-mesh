"""资料上传与批次协调服务（T047 / FR-004、FR-006、FR-031、FR-033）。

流程（data-model.md 批量上传/上传恢复/上传重放）：
1. 整批无副作用预校验（格式/大小/数量），任一失败整批拒绝；
2. 幂等键检查：已收敛返回首次快照；同键不同请求冲突 ``20008/409``；未超时
   coordinating 重放 ``20008/409`` 零副作用；超时后锁定并调用同一协调函数接管；
3. 生成 ``upload_batch_id``，写临时对象（数据库外），同一事务创建全部 pending
   资料、不可执行 pending 初始 parse 任务与可选 coordinating 幂等记录；
   数据库失败回滚、清理临时对象并返回 ``50000/500``；
4. 协调：``SELECT FOR UPDATE SKIP LOCKED`` 锁定批次，短事务内完成同卷原子转正；
   全部转正后单事务把三者切换 queued 并投递 parse、返回 ``202``；任一转正失败
   整批补偿 ``failed/20011``、清对象、零投递。
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import UploadFile
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import (
    DEFAULT_ERROR_MSG,
    RESOURCE_CONFLICT_MSG,
)
from app.api.v1.schemas.documents import ASYNC_ERROR_MESSAGES, document_upload_item_dto
from app.core.settings import Settings, get_settings
from app.infrastructure.storage.local import final_object_key
from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.enums import (
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    UploadRequestStatus,
)
from app.models.upload_request import DocumentUploadRequest
from app.repositories.base import require_knowledge_base
from app.repositories.document_tasks import DocumentTaskRepository
from app.repositories.documents import DocumentRepository
from app.repositories.upload_requests import UploadRequestRepository
from app.services.file_storage import FileStorage, default_file_storage
from app.services.upload_validation import (
    ValidatedUpload,
    new_document_ids,
    request_fingerprint,
    validate_upload_batch,
)
from app.workers.base import dispatch_task as _default_dispatch

_CONFLICT_CODE = 20008
_INTERNAL_CODE = 50000

_TASK_NAME = DocumentTaskType.PARSE.value


@dataclass(frozen=True)
class UploadOutcome:
    """上传收敛结果；``replay`` 表示命中了同键首次结果。"""

    items: tuple[dict, ...]
    replay: bool = False


class DocumentService:
    """批量上传、批次协调与补偿。"""

    def __init__(
        self,
        session: Session,
        file_storage: FileStorage | None = None,
        dispatch: Callable[[str, tuple], None] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.file_storage = file_storage or default_file_storage()
        self.dispatch = dispatch or _default_dispatch
        self.settings = settings or get_settings()
        self.documents = DocumentRepository(session)
        self.tasks = DocumentTaskRepository(session)
        self.upload_requests = UploadRequestRepository(session)

    # ------------------------------------------------------------------
    # 上传入口
    # ------------------------------------------------------------------
    def upload(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        files: list[UploadFile],
        idempotency_key: str | None = None,
    ) -> UploadOutcome:
        # 知识库归属校验前置（20002/404，不泄露资源存在性；避免探测请求读取文件内容）。
        require_knowledge_base(self.session, knowledge_base_id, user_id)
        validated = validate_upload_batch(files)
        if idempotency_key:
            existing = self.upload_requests.find(user_id, knowledge_base_id, idempotency_key)
            if existing is not None:
                return self._handle_existing_request(existing, validated)

        batch_id = uuid.uuid4()
        try:
            request = self._persist_batch(
                user_id, knowledge_base_id, validated, batch_id, idempotency_key
            )
        except Exception as exc:
            # 持久化失败：清理临时对象并收敛为 50000/500。
            self.session.rollback()
            self.file_storage.cleanup_batch(batch_id)
            if isinstance(exc, ApiError):
                raise
            raise ApiError(_INTERNAL_CODE, DEFAULT_ERROR_MSG, 500) from exc
        return self.coordinate_batch(batch_id, request)

    # ------------------------------------------------------------------
    # 幂等重放
    # ------------------------------------------------------------------
    def _handle_existing_request(
        self, request: DocumentUploadRequest, validated: list[ValidatedUpload]
    ) -> UploadOutcome:
        fingerprint = request_fingerprint(validated)
        if request.request_fingerprint != fingerprint:
            # 同键不同请求：不得复用首次结果。
            raise ApiError(_CONFLICT_CODE, RESOURCE_CONFLICT_MSG, 409)
        if request.status in (UploadRequestStatus.ACCEPTED, UploadRequestStatus.FAILED):
            # 已收敛：复用首次结果，不重复创建资料、任务或文件对象。
            snapshot = request.response_snapshot.get("items", [])  # type: ignore[union-attr]
            return UploadOutcome(items=tuple(snapshot), replay=True)
        if request.expires_at > datetime.now(UTC):  # noqa: E501
            # 仍在协调窗口内：冲突且零副作用。
            raise ApiError(_CONFLICT_CODE, RESOURCE_CONFLICT_MSG, 409)
        # 超时：与恢复扫描器相同的幂等协调函数接管。
        return self.coordinate_batch(request.upload_batch_id, request)

    # ------------------------------------------------------------------
    # 持久化批次
    # ------------------------------------------------------------------
    def _persist_batch(
        self,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        validated: list[ValidatedUpload],
        batch_id: uuid.UUID,
        idempotency_key: str | None,
    ) -> DocumentUploadRequest | None:
        """写临时对象 + 单事务创建资料/初始任务/幂等记录。"""
        document_ids = new_document_ids(len(validated))
        self.file_storage.store_batch_temporaries(
            batch_id,
            [(doc_id, item.content) for doc_id, item in zip(document_ids, validated, strict=True)],
        )
        now = datetime.now(UTC)
        for doc_id, item in zip(document_ids, validated, strict=True):
            self.session.add(
                Document(
                    id=doc_id,  # 显式 ID：对象键、任务引用与资料行必须一致
                    user_id=user_id,
                    knowledge_base_id=knowledge_base_id,
                    filename=item.filename,
                    file_type=item.file_type,
                    file_size=item.file_size,
                    storage_path=final_object_key(batch_id, doc_id),
                    upload_batch_id=batch_id,
                    content_hash=item.content_hash,
                    status=DocumentStatus.PENDING,
                )
            )
        # 先落盘资料行再创建任务：document_task 映射器先于 document 注册，
        # 依赖原始 FK 列（无 relationship）时需显式 flush 保证插入顺序。
        self.session.flush()
        for doc_id, _item in zip(document_ids, validated, strict=True):
            self.tasks.create_initial_parse(
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                document_id=doc_id,
                document_version=1,
                status=DocumentTaskStatus.PENDING,
                queued_at=None,
            )
        request = None
        if idempotency_key:
            request = self.upload_requests.create(
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint(validated),
                upload_batch_id=batch_id,
                expires_at=now
                + timedelta(seconds=self.settings.storage.upload_idempotency_ttl_seconds),
            )
        try:
            self.session.commit()
        except IntegrityError:
            # 并发同键请求命中 (user, kb, key) 唯一约束：整批拒绝并映射
            # coordinating 冲突 20008/409（零副作用，临时对象已清理）。
            self.session.rollback()
            self.file_storage.cleanup_batch(batch_id)
            raise ApiError(_CONFLICT_CODE, RESOURCE_CONFLICT_MSG, 409) from None
        return request

    # ------------------------------------------------------------------
    # 批次协调（上传、重放接管与恢复扫描器共用）
    # ------------------------------------------------------------------
    def coordinate_batch(
        self, batch_id: uuid.UUID, request: DocumentUploadRequest | None
    ) -> UploadOutcome:
        """短事务锁定批次 → 同卷原子转正 → 单事务切换 queued/补偿。

        上传、超时重放接管与恢复扫描器共用的幂等协调函数。
        """
        docs = self.documents.lock_batch_for_coordination(batch_id)
        if not docs:
            # 锁不可得（他方正在协调）：不并发协调（data-model.md 上传恢复）。
            raise ApiError(_CONFLICT_CODE, RESOURCE_CONFLICT_MSG, 409)
        try:
            self.file_storage.promote_batch(batch_id, [doc.id for doc in docs])
        except FileNotFoundError:
            self.session.rollback()
            return self._compensate_batch(batch_id, docs, request)
        except OSError:
            self.session.rollback()
            return self._compensate_batch(batch_id, docs, request)

        self._mark_batch_queued(batch_id, docs, request)
        for task in self._parse_tasks(batch_id):
            self._dispatch_parse(task.id)
        # 提交后重新读取，DTO 反映 queued 终态。
        fresh = self.session.scalars(
            select(Document).where(Document.id.in_([d.id for d in docs])).order_by(Document.id)
        ).all()
        return UploadOutcome(items=tuple(document_upload_item_dto(doc) for doc in fresh))

    def _mark_batch_queued(
        self, batch_id: uuid.UUID, docs: list[Document], request: DocumentUploadRequest | None
    ) -> None:
        """单事务把整批资料、任务与幂等快照原子切换为 queued。"""
        now = datetime.now(UTC)
        doc_ids = [doc.id for doc in docs]
        self.session.execute(
            update(Document)
            .where(
                Document.id.in_(doc_ids),
                Document.status == DocumentStatus.PENDING,
            )
            .values(status=DocumentStatus.QUEUED, updated_at=now)
        )
        self.session.execute(
            update(DocumentTask)
            .where(
                DocumentTask.document_id.in_(doc_ids),
                DocumentTask.task_type == DocumentTaskType.PARSE,
                DocumentTask.status == DocumentTaskStatus.PENDING,
            )
            .values(status=DocumentTaskStatus.QUEUED, queued_at=now, updated_at=now)
        )
        if request is not None:
            # 提交前重新读取，快照必须反映 queued 终态（批量更新不刷新 ORM 对象）。
            fresh = self.session.scalars(
                select(Document).where(Document.id.in_(doc_ids)).order_by(Document.id)
            ).all()
            self.upload_requests.update_converged(
                request,
                status=UploadRequestStatus.ACCEPTED,
                snapshot={
                    "status": "accepted",
                    "items": [document_upload_item_dto(doc) for doc in fresh],
                },
                now=now,
            )
        self.session.commit()

    def _compensate_batch(
        self, batch_id: uuid.UUID, docs: list[Document], request: DocumentUploadRequest | None
    ) -> UploadOutcome:
        """任一对象转正失败：整批补偿为 failed/20011，清对象，零投递。"""
        self.file_storage.cleanup_batch(batch_id)
        now = datetime.now(UTC)
        message = ASYNC_ERROR_MESSAGES[20011]
        doc_ids = [doc.id for doc in docs]
        self.session.execute(
            update(Document)
            .where(Document.id.in_(doc_ids))
            .values(
                status=DocumentStatus.FAILED,
                current_task_type=DocumentTaskType.PARSE,
                error_code=20011,
                error_message=message,
                processing_finished_at=now,
                updated_at=now,
            )
        )
        self.session.execute(
            update(DocumentTask)
            .where(
                DocumentTask.document_id.in_(doc_ids),
                DocumentTask.task_type == DocumentTaskType.PARSE,
            )
            .values(
                status=DocumentTaskStatus.FAILED,
                error_code=20011,
                error_message=message,
                finished_at=now,
                updated_at=now,
            )
        )
        if request is not None:
            failed_items = [
                document_upload_item_dto(
                    self.session.scalar(select(Document).where(Document.id == doc_id))
                )
                for doc_id in doc_ids
            ]
            self.upload_requests.update_converged(
                request,
                status=UploadRequestStatus.FAILED,
                snapshot={"status": "failed", "items": failed_items},
                now=now,
            )
        self.session.commit()
        docs = list(
            self.session.scalars(
                select(Document).where(Document.id.in_(doc_ids)).order_by(Document.id)
            )
        )
        return UploadOutcome(items=tuple(document_upload_item_dto(doc) for doc in docs))

    def _parse_tasks(self, batch_id: uuid.UUID) -> list[DocumentTask]:
        return list(
            self.session.scalars(
                select(DocumentTask)
                .join(Document, Document.id == DocumentTask.document_id)
                .where(
                    Document.upload_batch_id == batch_id,
                    DocumentTask.task_type == DocumentTaskType.PARSE,
                )
                .order_by(DocumentTask.document_id)
            )
        )

    def _dispatch_parse(self, task_id: uuid.UUID) -> None:
        try:
            self.dispatch("orionamesh.document_parse", (task_id,))
        except Exception:  # noqa: BLE001 - 投递失败由恢复扫描器重投，DB 真相不丢
            self.session.rollback()
