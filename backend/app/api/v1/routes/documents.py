"""资料路由（T048/T080 / openapi.yaml knowledge-bases documents 段）。

- 批量上传：整批预校验后返回 ``202``；任一不合规整批拒绝且零副作用；
- 列表/详情：先固定排除内部 ``deleting/deleted`` 再应用公开 ``status`` 过滤，
  任何过滤参数不得绕过隐藏边界；内容与子资源读取只允许 ``active`` 知识库
  （``deleting``/``delete_failed`` 统一 ``20002/404``）；
- 详情与任务列表经 ``DocumentStatusService`` 暴露终态、契约限定的持久化失败码、
  失败原因与 ``allowed_actions``（``failed/delete_cleanup/20015`` 仅为
  ``retry_delete``）；
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
from app.api.v1.schemas.documents import PublicDocumentStatus
from app.infrastructure.database.session import get_db
from app.models.enums import DocumentStatus
from app.models.user import User
from app.services.document_deletion_service import DocumentDeletionService
from app.services.document_service import DocumentService
from app.services.document_status_service import DocumentStatusService

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
    result = DocumentStatusService(db).list_documents(
        knowledge_base_id,
        current_user.id,
        page=page,
        page_size=page_size,
        status=DocumentStatus(status.value) if status else None,
    )
    return success_response(result).model_dump(mode="json")


@router.get("/knowledge-bases/{knowledge_base_id}/documents/{document_id}")
def get_document(
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    result = DocumentStatusService(db).get_document(knowledge_base_id, document_id, current_user.id)
    return success_response(result).model_dump(mode="json")


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
    result = DocumentStatusService(db).list_tasks(
        knowledge_base_id,
        document_id,
        current_user.id,
        page=page,
        page_size=page_size,
    )
    return success_response(result).model_dump(mode="json")
