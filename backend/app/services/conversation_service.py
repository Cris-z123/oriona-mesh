"""会话与消息服务（T065 / FR-013、FR-013a、FR-014、FR-019、FR-020）。

- 会话必须绑定当前用户有权访问的知识库（MVP 无纯聊天）；知识库不存在统一
  ``20002/404``，会话/消息/引用未命中统一 ``20007/404``；
- 本服务同时是 :class:`AnswerService` 的会话持久化端口（T071）：消息创建与
  assistant 终态收敛（streaming → completed/stop、failed/error、cancelled/cancelled
  单向，经 :class:`MessageTerminalState`）都在这里落地；
- 消息与引用写入在提交前完成；会话列表/详情/分页与删除按当前用户隔离。
"""

import uuid

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message
from app.models.enums import MessageFinishReason, MessageStatus
from app.repositories.base import require_active_knowledge_base
from app.repositories.conversations import ConversationRepository
from app.services.message_terminal_state import MessageTerminalState


class ConversationService:
    """会话/消息/引用用例（实现 AnswerService 的会话端口）。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = ConversationRepository(session)

    # ------------------------------------------------------------------
    # 会话 CRUD
    # ------------------------------------------------------------------
    def create(
        self, user_id: uuid.UUID, knowledge_base_id: uuid.UUID, title: str | None = None
    ) -> tuple[Conversation, str]:
        # 对话只能绑定 active 知识库（deleting/delete_failed 隐藏，20002/404）。
        kb = require_active_knowledge_base(self.session, knowledge_base_id, user_id)
        return self.repository.create(user_id, knowledge_base_id, title), kb.name

    def get(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> tuple[Conversation, str]:
        conv = self.repository.get_for_user(conversation_id, user_id)
        return conv, self.repository.knowledge_base_name_for(user_id, conv.knowledge_base_id)

    def list_conversations(
        self,
        user_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
        knowledge_base_id: uuid.UUID | None = None,
    ) -> tuple[list[tuple[Conversation, str]], int]:
        return self.repository.list_for_user(
            user_id,
            page=page,
            page_size=page_size,
            knowledge_base_id=knowledge_base_id,
        )

    def rename(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, title: str
    ) -> tuple[Conversation, str]:
        conv = self.repository.rename(conversation_id, user_id, title)
        return conv, self.repository.knowledge_base_name_for(user_id, conv.knowledge_base_id)

    def delete(self, user_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        self.repository.delete(conversation_id, user_id)

    def list_messages(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        before: uuid.UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[Message], bool, uuid.UUID | None]:
        # 跨用户会话统一 20007/404，不返回空列表（FR-020）。
        self.repository.get_for_user(conversation_id, user_id)
        return self.repository.list_messages(conversation_id, user_id, before=before, limit=limit)

    # ------------------------------------------------------------------
    # AnswerService 会话端口
    # ------------------------------------------------------------------
    def create_user_message(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        content: str,
        rewritten_query: str | None = None,
    ) -> None:
        self.repository.create_user_message(user_id, conversation_id, content, rewritten_query)
        self.repository.update_last_message_at(conversation_id, user_id)
        self.session.commit()

    def create_streaming_assistant_message(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> uuid.UUID:
        message = self.repository.create_streaming_assistant_message(user_id, conversation_id)
        self.session.commit()
        return message.id

    def set_terminal(
        self,
        *,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        status: MessageStatus,
        finish_reason: MessageFinishReason | None,
        content: str | None = None,
    ) -> None:
        MessageTerminalState(self.session).set_terminal(
            message_id=message_id,
            user_id=user_id,
            status=status,
            finish_reason=finish_reason,
            content=content,
        )

    def recent_history(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, turns: int = 3
    ) -> list[tuple[str, str]]:
        return self.repository.recent_history(user_id, conversation_id, turns=turns)

    def update_last_message_at(self, *, user_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        self.repository.update_last_message_at(conversation_id, user_id)
