"""assistant 消息终态收敛器（T074 / FR-018、data-model.md 对话与消息）。

- 任何分支都必须离开 ``streaming``：正常完成及可信无证据为 ``completed/stop``，
  供应商/模型/服务错误重试耗尽为 ``failed/error``，客户端连接断开为
  ``cancelled/cancelled``；
- 单向收敛：只在消息仍为 ``streaming`` 时写入终态；迟到完成/失败不得覆盖
  已收敛的终态（连接断开后 worker 的迟到写入被忽略）；
- 维护扫描器复用本收敛器把超过 ``MESSAGE_STREAMING_STALE_SECONDS`` 的
  ``streaming`` 消息原子收敛为 ``failed/error``，不得覆盖已终态消息。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Message
from app.models.enums import MessageFinishReason, MessageStatus


class MessageTerminalState:
    """assistant 消息 streaming → 明确终态的单向收敛器。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def set_terminal(
        self,
        *,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        status: MessageStatus,
        finish_reason: MessageFinishReason | None,
        content: str | None = None,
    ) -> bool:
        """在锁内把仍为 streaming 的消息收敛为终态；已终态返回 False（不覆盖）。"""
        message = self.session.scalar(
            select(Message)
            .where(Message.id == message_id, Message.user_id == user_id)
            .with_for_update()
        )
        if message is None or message.status != MessageStatus.STREAMING:
            return False
        message.status = status
        message.finish_reason = finish_reason
        if content is not None:
            message.content = content
        self.session.commit()
        return True

    def complete(
        self,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        content: str,
        finish_reason: MessageFinishReason | str = "stop",
    ) -> bool:
        return self.set_terminal(
            message_id=message_id,
            user_id=user_id,
            status=MessageStatus.COMPLETED,
            finish_reason=MessageFinishReason(finish_reason),
            content=content,
        )

    def fail(self, message_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return self.set_terminal(
            message_id=message_id,
            user_id=user_id,
            status=MessageStatus.FAILED,
            finish_reason=MessageFinishReason.ERROR,
        )

    def cancel(self, message_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return self.set_terminal(
            message_id=message_id,
            user_id=user_id,
            status=MessageStatus.CANCELLED,
            finish_reason=MessageFinishReason.CANCELLED,
        )
