"""会话路由（T066 / openapi.yaml conversations 段）。

- 会话 CRUD 必须绑定当前用户有权访问的知识库；知识库未命中 ``20002/404``，
  会话/消息/引用未命中统一 ``20007/404``；
- 消息使用 ``before/limit`` 游标分页（has_more/next_before 连续无重复）；
- 引用按 rank 升序分页，统一 Citation DTO 由 ``citation_service`` 构造。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.dependencies.auth import get_current_user
from app.api.v1.schemas.common import success_response
from app.api.v1.schemas.conversations import (
    CreateConversationInput,
    RenameConversationInput,
    conversation_dto,
    message_dto,
)
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.services.citation_service import CitationService
from app.services.conversation_service import ConversationService

router = APIRouter()


@router.post("/conversations", status_code=201)
def create_conversation(
    payload: CreateConversationInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv, knowledge_base_name = ConversationService(db).create(
        current_user.id, payload.knowledge_base_id, payload.title
    )
    db.commit()
    return success_response(conversation_dto(conv, knowledge_base_name)).model_dump(mode="json")


@router.get("/conversations")
def list_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    knowledge_base_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    items, total = ConversationService(db).list_conversations(
        current_user.id,
        page=page,
        page_size=page_size,
        knowledge_base_id=knowledge_base_id,
    )
    return success_response(
        {
            "items": [conversation_dto(conv, name) for conv, name in items],
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    ).model_dump(mode="json")


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv, knowledge_base_name = ConversationService(db).get(current_user.id, conversation_id)
    return success_response(conversation_dto(conv, knowledge_base_name)).model_dump(mode="json")


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: uuid.UUID,
    payload: RenameConversationInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv, knowledge_base_name = ConversationService(db).rename(
        current_user.id, conversation_id, payload.title
    )
    db.commit()
    return success_response(conversation_dto(conv, knowledge_base_name)).model_dump(mode="json")


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    ConversationService(db).delete(current_user.id, conversation_id)
    db.commit()
    return success_response(None).model_dump(mode="json")


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: uuid.UUID,
    before: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    items, has_more, next_before = ConversationService(db).list_messages(
        current_user.id, conversation_id, before=before, limit=limit
    )
    return success_response(
        {
            "items": [message_dto(message) for message in items],
            "has_more": has_more,
            "next_before": str(next_before) if next_before else None,
        }
    ).model_dump(mode="json")


@router.get("/conversations/{conversation_id}/messages/{message_id}/citations")
def list_citations(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    items, total = CitationService(db).list_for_message(
        message_id,
        conversation_id,
        current_user.id,
        page=page,
        page_size=page_size,
    )
    return success_response(
        {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    ).model_dump(mode="json")
