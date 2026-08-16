"""资料路由（T048 / openapi.yaml knowledge-bases documents 段）。

- 批量上传：整批预校验后返回 ``202``；任一不合规整批拒绝且零副作用；
- 列表/详情：先固定排除内部 ``deleting/deleted`` 再应用公开 ``status`` 过滤，
  任何过滤参数不得绕过隐藏边界；
- 任务列表返回完整 Attempt DTO（worker、非空 started_at、可空终态字段）；
- 删除（T057 编排）：首次删除隐藏并创建 delete_cleanup，重复删除幂等，
  ``failed/delete_cleanup/20015`` 重试才新建删除轮次。
"""

import re
import uuid

from fastapi import APIRouter, Depends, File, Header, Query, UploadFile
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.dependencies.auth import get_current_user
from app.api.v1.schemas.common import (
    VALIDATION_ERROR_MSG,
    success_response,
)
from app.api.v1.schemas.documents import (
    PublicDocumentStatus,
    document_dto,
    document_task_dto,
)
from app.infrastructure.database.session import get_db
from app.models.enums import DocumentStatus
from app.models.user import User
from app.repositories.base import require_knowledge_base
from app.repositories.document_task_attempts import DocumentTaskAttemptRepository
from app.repositories.document_tasks import DocumentTaskRepository
from app.repositories.documents import DocumentRepository
from app.services.document_deletion_service import DocumentDeletionService
from app.services.document_service import DocumentService

router = APIRouter()

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


@router.post("/knowledge-bases/{knowledge_base_id}/documents", status_code=202)
def upload_documents(
    knowledge_base_id: uuid.UUID,
    files: list[UploadFile] = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if idempotency_key is not None:
        if not (8 <= len(idempotency_key) <= 128) or not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
            raise ApiError(10003, VALIDATION_ERROR_MSG, 400)
    outcome = DocumentService(db).upload(
        current_user.id, knowledge_base_id, files, idempotency_key=idempotency_key
    )
    return success_response({"documents": list(outcome.items)}).model_dump(mode="json")


@router.get("/knowledge-bases/{knowledge_base_id}/documents")
def list_documents(
    knowledge_base_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status: PublicDocumentStatus | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    require_knowledge_base(db, knowledge_base_id, current_user.id)
    items, total = DocumentRepository(db).list_visible(
        knowledge_base_id,
        current_user.id,
        page=page,
        page_size=page_size,
        status=DocumentStatus(status.value) if status else None,
    )
    return success_response(
        {
            "items": [document_dto(doc) for doc in items],
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    ).model_dump(mode="json")


@router.get("/knowledge-bases/{knowledge_base_id}/documents/{document_id}")
def get_document(
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    require_knowledge_base(db, knowledge_base_id, current_user.id)
    doc = DocumentRepository(db).get_visible(document_id, knowledge_base_id, current_user.id)
    return success_response(document_dto(doc)).model_dump(mode="json")


@router.delete("/knowledge-bases/{knowledge_base_id}/documents/{document_id}")
def delete_document(
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    DocumentDeletionService(db).delete(current_user.id, knowledge_base_id, document_id)
    return success_response(None).model_dump(mode="json")


@router.get("/knowledge-bases/{knowledge_base_id}/documents/{document_id}/tasks")
def list_document_tasks(
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    require_knowledge_base(db, knowledge_base_id, current_user.id)
    DocumentRepository(db).get_visible(document_id, knowledge_base_id, current_user.id)
    tasks, total = DocumentTaskRepository(db).list_for_document(
        document_id, current_user.id, page=page, page_size=page_size
    )
    attempts_repo = DocumentTaskAttemptRepository(db)
    items: list[dict] = []
    for task in tasks:
        items.append(document_task_dto(task, attempts_repo.list_for_task(task.id, current_user.id)))
    return success_response(
        {"items": items, "page": page, "page_size": page_size, "total": total}
    ).model_dump(mode="json")
