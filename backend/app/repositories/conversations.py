"""会话、消息与引用仓储（T065 / FR-013、FR-019、FR-020、data-model.md 对话与消息）。

- 所有读取以当前 ``user_id`` 为强制边界；未命中统一 ``20007/404``，禁止全局探测；
- 消息游标分页按 ``(created_at, id)`` 倒序键控，``before`` 为游标消息 ID；
- 引用行经 ``message_id`` 归属校验后读取，删除后的来源 ID 由外键置空并由
  ``citation_service`` 回退快照；
- assistant 消息终态收敛统一经 ``MessageTerminalState``（streaming → 终态单向）。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import RESOURCE_NOT_FOUND_MSG
from app.models.conversation import Conversation, Message, MessageCitation
from app.models.enums import MessageRole, MessageStatus


@dataclass(frozen=True)
class CitationDraft:
    """回答完成时保存的引用（T072：chunk_id/document_id + 必填快照）。"""

    chunk_id: uuid.UUID | None
    document_id: uuid.UUID | None
    document_version: int
    rank: int
    score: float
    snapshot: dict


class ConversationRepository:
    """以 user_id 为边界的会话/消息/引用仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def get_for_user(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> Conversation:
        conv = self.session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id
            )
        )
        if conv is None:
            raise ApiError(20007, RESOURCE_NOT_FOUND_MSG, 404)
        return conv

    def list_for_user(
        self, user_id: uuid.UUID, *, page: int, page_size: int
    ) -> tuple[list[Conversation], int]:
        total = self.session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
        )
        rows = self.session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(rows), int(total or 0)

    def create(
        self, user_id: uuid.UUID, knowledge_base_id: uuid.UUID, title: str | None = None
    ) -> Conversation:
        conv = Conversation(user_id=user_id, knowledge_base_id=knowledge_base_id, title=title)
        self.session.add(conv)
        self.session.flush()
        return conv

    def rename(self, conversation_id: uuid.UUID, user_id: uuid.UUID, title: str) -> Conversation:
        conv = self.get_for_user(conversation_id, user_id)
        conv.title = title
        self.session.flush()
        return conv

    def delete(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self.get_for_user(conversation_id, user_id)
        self.session.execute(
            delete(Message).where(
                Message.conversation_id == conversation_id, Message.user_id == user_id
            )
        )
        self.session.execute(
            delete(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id
            )
        )
        self.session.flush()

    def update_last_message_at(self, conversation_id: uuid.UUID, user_id: uuid.UUID) -> None:
        conv = self.get_for_user(conversation_id, user_id)
        conv.last_message_at = datetime.now(UTC)
        self.session.flush()

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    def list_messages(
        self,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        before: uuid.UUID | None = None,
        limit: int = 50,
    ) -> tuple[list[Message], bool, uuid.UUID | None]:
        """游标分页：按 ``(created_at, id)`` 倒序，返回 (items, has_more, next_before)。"""
        stmt = select(Message).where(
            Message.conversation_id == conversation_id, Message.user_id == user_id
        )
        if before is not None:
            cursor = self.get_message(before, conversation_id, user_id)
            stmt = stmt.where(
                tuple_(Message.created_at, Message.id) < (cursor.created_at, cursor.id)
            )
        rows = self.session.scalars(
            stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        items = list(rows[:limit])
        next_before = items[-1].id if has_more and items else None
        return items, has_more, next_before

    def get_message(
        self, message_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID
    ) -> Message:
        message = self.session.scalar(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.user_id == user_id,
            )
        )
        if message is None:
            raise ApiError(20007, RESOURCE_NOT_FOUND_MSG, 404)
        return message

    def create_user_message(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        content: str,
        rewritten_query: str | None = None,
    ) -> Message:
        message = Message(
            user_id=user_id,
            conversation_id=conversation_id,
            role=MessageRole.USER,
            status=MessageStatus.COMPLETED,
            content=content,
            rewritten_query=rewritten_query,
        )
        self.session.add(message)
        self.session.flush()
        return message

    def create_streaming_assistant_message(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Message:
        message = Message(
            user_id=user_id,
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            content="",
        )
        self.session.add(message)
        self.session.flush()
        return message

    def recent_history(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, *, turns: int = 3
    ) -> list[tuple[str, str]]:
        """最近三轮（含当前问题之前的最后 turns 条消息）最小上下文。"""
        rows = self.session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.user_id == user_id,
                Message.status != MessageStatus.STREAMING,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(turns * 2)  # 三轮对话 = 最多 3 user + 3 assistant
        ).all()
        return [(m.role.value, m.content) for m in reversed(rows)]

    def list_citations(
        self,
        message_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[MessageCitation], int]:
        self.get_message(message_id, conversation_id, user_id)
        total = self.session.scalar(
            select(func.count())
            .select_from(MessageCitation)
            .where(
                MessageCitation.message_id == message_id,
                MessageCitation.user_id == user_id,
            )
        )
        rows = self.session.scalars(
            select(MessageCitation)
            .where(
                MessageCitation.message_id == message_id,
                MessageCitation.user_id == user_id,
            )
            .order_by(MessageCitation.rank.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return list(rows), int(total or 0)

    def save_citations(
        self,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        drafts: list[CitationDraft],
    ) -> None:
        for draft in drafts:
            self.session.add(
                MessageCitation(
                    message_id=message_id,
                    user_id=user_id,
                    knowledge_base_id=knowledge_base_id,
                    chunk_id=draft.chunk_id,
                    document_id=draft.document_id,
                    document_version=draft.document_version,
                    rank=draft.rank,
                    score=draft.score,
                    chunk_snapshot=draft.snapshot,
                )
            )
        self.session.flush()
