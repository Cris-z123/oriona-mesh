"""知识库路由（T022/T081 / openapi.yaml knowledge-bases 段）。

- 列表/详情以所属用户为范围；跨用户统一 ``20002/404``，不泄露资源归属；
- 删除（T081）：空知识库直接物理删除；非空知识库置 ``deleting`` 并编排全部
  子资料的有界删除，提交后立即隐藏；``delete_failed/20015`` 最小墓碑仅返回
  ``retry_delete``，再次 DELETE 才转回 ``deleting``。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.dependencies.auth import get_current_user
from app.api.v1.schemas.common import success_response
from app.api.v1.schemas.knowledge_bases import (
    KnowledgeBaseInput,
    UpdateKnowledgeBaseInput,
    knowledge_base_dto,
)
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.services.knowledge_base_service import KnowledgeBaseService

router = APIRouter()


@router.get("/knowledge-bases")
def list_knowledge_bases(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    items, total = KnowledgeBaseService(db).list_for_user(
        current_user.id, page=page, page_size=page_size
    )
    return success_response(
        {
            "items": [knowledge_base_dto(kb) for kb in items],
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    ).model_dump(mode="json")


@router.post("/knowledge-bases", status_code=201)
def create_knowledge_base(
    payload: KnowledgeBaseInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    kb = KnowledgeBaseService(db).create(current_user.id, payload.name, payload.description)
    return success_response(knowledge_base_dto(kb)).model_dump(mode="json")


@router.get("/knowledge-bases/{knowledge_base_id}")
def get_knowledge_base(
    knowledge_base_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    kb = KnowledgeBaseService(db).get(current_user.id, knowledge_base_id)
    return success_response(knowledge_base_dto(kb)).model_dump(mode="json")


@router.patch("/knowledge-bases/{knowledge_base_id}")
def update_knowledge_base(
    knowledge_base_id: uuid.UUID,
    payload: UpdateKnowledgeBaseInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    kb = KnowledgeBaseService(db).update(
        current_user.id,
        knowledge_base_id,
        name=payload.name,
        description=payload.description,
    )
    return success_response(knowledge_base_dto(kb)).model_dump(mode="json")


@router.delete("/knowledge-bases/{knowledge_base_id}")
def delete_knowledge_base(
    knowledge_base_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    KnowledgeBaseService(db).delete(current_user.id, knowledge_base_id)
    return success_response(None).model_dump(mode="json")
