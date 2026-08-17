"""SSE 终态与失联恢复集成测试（T063 / FR-018、T074 维护扫描器扩展）。

覆盖：API 进程中断后维护扫描器只条件收敛超过 ``MESSAGE_STREAMING_STALE_SECONDS``
的 ``streaming`` assistant 消息为 ``failed/error``，不覆盖已终态消息、用户消息、
新鲜 streaming 或终态引用消息；终态收敛函数幂等且不会在收敛后再次改写。
需要真实 PostgreSQL。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message, MessageCitation
from app.models.enums import (
    MessageFinishReason,
    MessageRole,
    MessageStatus,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User

pytestmark = pytest.mark.integration


def _user(db: Session, email: str) -> User:
    user = User(email=email, password_hash="x" * 60)
    db.add(user)
    db.flush()
    return user


def _conv(db: Session, user: User) -> tuple[KnowledgeBase, Conversation]:
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db.add(kb)
    db.flush()
    conv = Conversation(user_id=user.id, knowledge_base_id=kb.id)
    db.add(conv)
    db.flush()
    return kb, conv


def _message(
    db: Session,
    conv: Conversation,
    *,
    role: MessageRole,
    status: MessageStatus,
    finish_reason: MessageFinishReason | None,
    created_at: datetime,
    content: str = "x",
) -> Message:
    msg = Message(
        user_id=conv.user_id,
        conversation_id=conv.id,
        role=role,
        status=status,
        finish_reason=finish_reason,
        content=content,
        created_at=created_at,
    )
    db.add(msg)
    db.flush()
    return msg


def _stale_scan(db: Session, stale_seconds: int = 360) -> int:
    """以给定失联阈值运行 streaming 消息收敛扫描（T074 维护扫描器扩展）。"""
    from app.workers.task_recovery import converge_stale_streaming_messages

    return converge_stale_streaming_messages(db, now=datetime.now(UTC), stale_seconds=stale_seconds)


class TestStaleStreamingConvergence:
    def test_only_stale_streaming_messages_converged_to_failed_error(
        self, db_session: Session
    ) -> None:
        user = _user(db_session, "sse-scan@example.com")
        kb, conv = _conv(db_session, user)
        now = datetime.now(UTC)
        stale = _message(
            db_session,
            conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            finish_reason=None,
            created_at=now - timedelta(seconds=1000),
        )
        fresh = _message(
            db_session,
            conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            finish_reason=None,
            created_at=now - timedelta(seconds=60),
        )
        completed = _message(
            db_session,
            conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            finish_reason=MessageFinishReason.STOP,
            created_at=now - timedelta(seconds=2000),
        )
        failed = _message(
            db_session,
            conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.FAILED,
            finish_reason=MessageFinishReason.ERROR,
            created_at=now - timedelta(seconds=2000),
        )
        cancelled = _message(
            db_session,
            conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.CANCELLED,
            finish_reason=MessageFinishReason.CANCELLED,
            created_at=now - timedelta(seconds=2000),
        )
        user_msg = _message(
            db_session,
            conv,
            role=MessageRole.USER,
            status=MessageStatus.COMPLETED,
            finish_reason=None,
            created_at=now - timedelta(seconds=2000),
            content="问题",
        )
        db_session.commit()

        # 阈值 300 秒：stale（1000 秒）超限，fresh（60 秒）未超限。
        converged = _stale_scan(db_session, stale_seconds=300)
        assert converged == 1
        db_session.refresh(stale)
        db_session.refresh(fresh)
        assert stale.status == MessageStatus.FAILED
        assert stale.finish_reason == MessageFinishReason.ERROR
        # 其他消息均不被改写。
        assert fresh.status == MessageStatus.STREAMING
        assert fresh.finish_reason is None
        assert completed.status == MessageStatus.COMPLETED
        assert completed.finish_reason == MessageFinishReason.STOP
        assert failed.status == MessageStatus.FAILED
        assert cancelled.status == MessageStatus.CANCELLED
        assert user_msg.status == MessageStatus.COMPLETED

    def test_scan_is_idempotent_and_scoped_to_user_conversations(self, db_session) -> None:
        user = _user(db_session, "sse-scan2@example.com")
        other = _user(db_session, "sse-scan-other@example.com")
        kb, conv = _conv(db_session, user)
        _, other_conv = _conv(db_session, other)
        now = datetime.now(UTC)
        stale = _message(
            db_session,
            conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            finish_reason=None,
            created_at=now - timedelta(seconds=1000),
        )
        _message(
            db_session,
            other_conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            finish_reason=None,
            created_at=now - timedelta(seconds=1000),
        )
        db_session.commit()
        # 扫描器是全库维护动作：收敛所有超过阈值的 streaming（跨用户，无权限概念）。
        assert _stale_scan(db_session, stale_seconds=300) == 2
        # 再次扫描：已收敛不再命中。
        assert _stale_scan(db_session, stale_seconds=300) == 0
        db_session.refresh(stale)
        assert stale.status == MessageStatus.FAILED
        assert stale.finish_reason == MessageFinishReason.ERROR

    def test_streaming_with_citations_is_also_converged(self, db_session) -> None:
        user = _user(db_session, "sse-scan3@example.com")
        kb, conv = _conv(db_session, user)
        now = datetime.now(UTC)
        msg = _message(
            db_session,
            conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            finish_reason=None,
            created_at=now - timedelta(seconds=1000),
        )
        db_session.add(
            MessageCitation(
                message_id=msg.id,
                user_id=user.id,
                knowledge_base_id=kb.id,
                chunk_id=None,
                document_id=None,
                document_version=1,
                rank=1,
                score=0.5,
                chunk_snapshot={"filename": "x.txt", "file_type": "txt", "content": "x"},
            )
        )
        db_session.commit()
        assert _stale_scan(db_session, stale_seconds=300) == 1
        db_session.refresh(msg)
        assert msg.status == MessageStatus.FAILED
        assert msg.finish_reason == MessageFinishReason.ERROR


class TestClientDisconnectConvergence:
    """客户端断开收敛：直接 aclose() 异步生成器模拟连接关闭（生产 uvicorn 在
    send 时抛 ClientDisconnected 并关闭生成器，触发 finally 收敛 cancelled）。"""

    def _answer_service(self, db: Session, conv: Conversation, generation=None):
        import uuid as _uuid

        from app.infrastructure.model_gateway.types import GenerationDelta
        from app.repositories.chunks import RetrievalChunk
        from app.services.answer_service import AnswerService
        from app.services.conversation_service import ConversationService
        from app.services.retrieval_service import RetrievalResult

        class FakeRewrite:
            def rewrite(self, *, user_id, query, history) -> str:
                return query

        class FakeRetrieval:
            def count_retrievable(self, user_id, knowledge_base_id) -> int:
                return 1

            def retrieve(self, user_id, knowledge_base_id, query, *, trace_id=None):
                chunk = RetrievalChunk(
                    chunk_id=_uuid.uuid4(),
                    document_id=_uuid.uuid4(),
                    document_version=1,
                    seq=0,
                    content="来源内容",
                    filename="source.txt",
                    file_type="txt",
                    fused_score=0.9,
                )
                return RetrievalResult(query=query, candidates=(chunk,), context_pack="来源内容")

        class FakeGeneration:
            def stream(self, *, user_id, query, context_pack, history):
                # 先产出首个增量，再阻塞：制造“生成中途客户端断开”的窗口。
                yield GenerationDelta(text="根据")
                import time

                time.sleep(3600)

        class FakeCitations:
            def __init__(self) -> None:
                self.saved: list = []

            def save(self, *, message_id, user_id, knowledge_base_id, drafts) -> None:
                self.saved.append(list(drafts))

        return AnswerService(
            conversations=ConversationService(db),
            retrieval=FakeRetrieval(),
            rewrite=FakeRewrite(),
            generation=generation or FakeGeneration(),
            citations=FakeCitations(),
        )

    def test_disconnect_converges_cancelled_cancelled(self, db_session: Session) -> None:
        import asyncio

        from app.api.v1.sse.message_stream import stream_answer_events

        user = _user(db_session, "sse-disc@example.com")
        kb, conv = _conv(db_session, user)
        db_session.commit()
        answer = self._answer_service(db_session, conv)
        bundle = answer.prepare(
            user_id=user.id,
            knowledge_base_id=kb.id,
            conversation_id=conv.id,
            content="问题",
        )
        assert bundle.no_evidence is False

        async def _drive() -> None:
            from collections.abc import AsyncGenerator
            from typing import cast

            stream = cast(AsyncGenerator[str, None], stream_answer_events(answer, bundle))
            first = await stream.__anext__()  # message_start
            assert "message_start" in first
            second = await stream.__anext__()  # retrieval_done
            assert "retrieval_done" in second
            third = await stream.__anext__()  # 首个 delta（生成进行中）
            assert "delta" in third
            # 生成中途客户端断开：关闭异步生成器 → finally 收敛 cancelled/cancelled。
            await stream.aclose()

        asyncio.run(_drive())
        assistant = (
            db_session.query(Message)
            .filter_by(conversation_id=conv.id)
            .order_by(Message.created_at.desc())
            .first()
        )
        assert assistant is not None
        assert assistant.status == MessageStatus.CANCELLED
        assert assistant.finish_reason == MessageFinishReason.CANCELLED

    def test_disconnect_stops_background_generation(self, db_session: Session) -> None:
        # P1 资源边界：客户端断连后，后台生成线程必须停止拉取并关闭生成流，
        # 而不是继续消费模型流或永久阻塞在满队列上。
        import asyncio
        import threading
        import time

        from app.api.v1.sse.message_stream import stream_answer_events
        from app.infrastructure.model_gateway.types import GenerationDelta

        user = _user(db_session, "sse-disc-stop@example.com")
        kb, conv = _conv(db_session, user)
        db_session.commit()

        class CountingGeneration:
            """持续产出增量并记录拉取/关闭的生成端口。"""

            def __init__(self) -> None:
                self.pulls = 0
                self.closed = threading.Event()

            def stream(self, *, user_id, query, context_pack, history):
                def gen():
                    try:
                        while True:
                            self.pulls += 1
                            yield GenerationDelta(text="x")
                    finally:
                        self.closed.set()

                return gen()

        generation = CountingGeneration()
        answer = self._answer_service(db_session, conv, generation=generation)
        bundle = answer.prepare(
            user_id=user.id,
            knowledge_base_id=kb.id,
            conversation_id=conv.id,
            content="问题",
        )
        assert bundle.no_evidence is False

        async def _drive() -> None:
            from collections.abc import AsyncGenerator
            from typing import cast

            stream = cast(AsyncGenerator[str, None], stream_answer_events(answer, bundle))
            await stream.__anext__()  # message_start
            await stream.__anext__()  # retrieval_done
            await stream.__anext__()  # delta（生成进行中）
            await stream.aclose()  # 客户端断开

        asyncio.run(_drive())
        # 断连后生产者必须关闭生成流（等待上限内收敛）。
        assert generation.closed.wait(3.0), "generation stream must be closed on disconnect"
        # 关闭后不得继续拉取模型流。
        pulled_at_close = generation.pulls
        time.sleep(0.5)
        assert generation.pulls == pulled_at_close, "producer must stop pulling after disconnect"


class TestTerminalStateConvergence:
    def test_terminal_writer_leaves_message_in_exactly_one_terminal_state(self, db_session) -> None:
        from app.services.message_terminal_state import MessageTerminalState

        user = _user(db_session, "sse-terminal@example.com")
        kb, conv = _conv(db_session, user)
        now = datetime.now(UTC)
        msg = _message(
            db_session,
            conv,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            finish_reason=None,
            created_at=now,
        )
        db_session.commit()
        writer = MessageTerminalState(db_session)
        writer.cancel(msg.id, user.id)
        # 已收敛 cancelled 后，迟到的完成/失败写入不得覆盖。
        writer.complete(msg.id, user.id, content="late", finish_reason="stop")
        writer.fail(msg.id, user.id)
        db_session.refresh(msg)
        assert msg.status == MessageStatus.CANCELLED
        assert msg.finish_reason == MessageFinishReason.CANCELLED
        assert msg.content == "x"
