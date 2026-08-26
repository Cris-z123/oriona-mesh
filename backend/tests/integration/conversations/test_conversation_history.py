"""全局会话历史仓储契约回归（T170 / FR-013）。

覆盖：列表按 ``updated_at DESC, created_at DESC`` 最近活动排序（含并列时间戳的稳定次序）；
``knowledge_base_name`` 经当前用户范围的授权连接投影，绝不透出其他用户的同名知识库；
``knowledge_base_name`` 不是 ``conversations`` 的持久化冗余列（data-model.md 约束）。
需要真实 PostgreSQL。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.repositories.conversations import ConversationRepository

pytestmark = pytest.mark.integration


def _user(db: Session, email: str) -> User:
    user = User(email=email, password_hash="x" * 60)
    db.add(user)
    db.flush()
    return user


class TestGlobalHistoryOrdering:
    def test_updated_at_desc_with_created_at_tiebreak(self, db_session: Session) -> None:
        user = _user(db_session, "order@example.com")
        kb = KnowledgeBase(user_id=user.id, name="kb")
        db_session.add(kb)
        db_session.flush()
        # 同一事务内 func.now() 的事务时间戳相同，必须显式设置 created_at 制造并列
        # updated_at 时的稳定 tiebreak。
        base = datetime.now(UTC) + timedelta(days=1)
        first = Conversation(user_id=user.id, knowledge_base_id=kb.id, title="first")
        first.created_at = base - timedelta(days=2)
        db_session.add(first)
        db_session.flush()
        second = Conversation(user_id=user.id, knowledge_base_id=kb.id, title="second")
        second.created_at = base - timedelta(days=1)
        db_session.add(second)
        db_session.flush()
        third = Conversation(user_id=user.id, knowledge_base_id=kb.id, title="third")
        third.created_at = base
        db_session.add(third)
        db_session.flush()
        # first/second 推进到同一“最近活动”时间；third 保留最旧活动，应排最后。
        first.updated_at = base
        second.updated_at = base
        db_session.commit()

        rows, total = ConversationRepository(db_session).list_for_user(
            user.id, page=1, page_size=10
        )
        assert total == 3
        # 并列 updated_at 时按 created_at 倒序；first 比 second 更早创建。
        assert [conv.id for conv, _ in rows] == [second.id, first.id, third.id]


class TestKnowledgeBaseNameProjection:
    def test_join_scoped_to_conversation_owner(self, db_session: Session) -> None:
        owner = _user(db_session, "owner@example.com")
        other = _user(db_session, "other@example.com")
        owner_kb = KnowledgeBase(user_id=owner.id, name="同名知识库")
        other_kb = KnowledgeBase(user_id=other.id, name="同名知识库")
        db_session.add_all([owner_kb, other_kb])
        db_session.flush()
        conv = Conversation(user_id=owner.id, knowledge_base_id=owner_kb.id)
        db_session.add(conv)
        db_session.commit()

        rows, _ = ConversationRepository(db_session).list_for_user(owner.id, page=1, page_size=10)
        assert rows == [(conv, "同名知识库")]

    def test_no_redundant_knowledge_base_name_column(self, test_engine) -> None:
        columns = {column["name"] for column in inspect(test_engine).get_columns("conversations")}
        assert "knowledge_base_name" not in columns
