"""会话/消息/引用 DTO（T066 / openapi.yaml Conversation/Message/Citation）。

- 会话标题与 last_message_at 均可空；消息按 role 判别（user 固定 completed，
  assistant 为 streaming 或明确终态）；
- Citation 由 ``citation_service`` 构造（live/snapshot 判别），路由只透传；
- 请求体与 openapi.yaml CreateConversationInput/RenameConversationInput/
  SendMessageInput 对齐（标题 1..200、内容 1..12000）。
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation import Conversation, Message


class CreateConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: uuid.UUID
    title: str | None = Field(default=None, min_length=1, max_length=200)


class RenameConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)


class SendMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=12000)


def conversation_dto(conv: Conversation) -> dict:
    return {
        "id": str(conv.id),
        "knowledge_base_id": str(conv.knowledge_base_id),
        "title": conv.title,
        "last_message_at": (conv.last_message_at.isoformat() if conv.last_message_at else None),
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
    }


def message_dto(message: Message) -> dict:
    return {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "role": message.role.value,
        "content": message.content,
        "status": message.status.value,
        "rewritten_query": message.rewritten_query,
        "finish_reason": (
            message.finish_reason.value if message.finish_reason is not None else None
        ),
        "created_at": message.created_at.isoformat(),
    }
