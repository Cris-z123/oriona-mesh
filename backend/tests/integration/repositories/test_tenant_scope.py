"""租户边界仓储集成测试（T021 / FR-020）。

覆盖：跨用户知识库统一 ``20002/404``、其他跨用户资源（资料/任务/任务尝试/对话/
引用）统一 ``20007/404``、禁止全局存在性探测与不泄露内容；读取固定过滤当前用户。
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.models.conversation import Conversation, Message, MessageCitation
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    FileType,
    MessageRole,
    MessageStatus,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.repositories.base import TenantScopedRepository, require_knowledge_base
from app.repositories.document_task_attempts import DocumentTaskAttemptRepository

pytestmark = pytest.mark.integration

_NOT_FOUND_MSG = "请求的资源不存在"
_KB_NOT_FOUND_MSG = "请求的知识库不存在"


@pytest.fixture()
def tenant_fixture(db_session: Session):
    """用户 A 与用户 B；用户 A 拥有知识库、资料、任务、对话、消息与引用。"""
    user_a = User(email="a@example.com", password_hash="h", display_name="A")
    user_b = User(email="b@example.com", password_hash="h", display_name="B")
    db_session.add_all([user_a, user_b])
    db_session.flush()
    kb = KnowledgeBase(user_id=user_a.id, name="kb-a")
    db_session.add(kb)
    db_session.flush()
    doc = Document(
        user_id=user_a.id,
        knowledge_base_id=kb.id,
        filename="a.pdf",
        file_type=FileType.PDF,
        file_size=100,
        storage_path="objs/a",
        upload_batch_id=uuid.uuid4(),
        content_hash="c",
        status=DocumentStatus.COMPLETED,
    )
    db_session.add(doc)
    db_session.flush()
    task = DocumentTask(
        user_id=user_a.id,
        knowledge_base_id=kb.id,
        document_id=doc.id,
        document_version=1,
        task_type=DocumentTaskType.PARSE,
        status=DocumentTaskStatus.SUCCEEDED,
        idempotency_key="parse:a:v1",
    )
    db_session.add(task)
    db_session.flush()
    attempt = DocumentTaskAttempt(
        task_id=task.id,
        user_id=user_a.id,
        knowledge_base_id=kb.id,
        document_id=doc.id,
        document_version=1,
        attempt_no=1,
        worker_name="w",
        started_at=datetime.now(UTC),
    )
    db_session.add(attempt)
    conv = Conversation(user_id=user_a.id, knowledge_base_id=kb.id, title="t")
    db_session.add(conv)
    db_session.flush()
    msg = Message(
        user_id=user_a.id,
        conversation_id=conv.id,
        role=MessageRole.USER,
        status=MessageStatus.COMPLETED,
        content="q",
    )
    db_session.add(msg)
    db_session.flush()
    citation = MessageCitation(
        message_id=msg.id,
        user_id=user_a.id,
        knowledge_base_id=kb.id,
        document_version=1,
        rank=1,
        score=0.9,
        chunk_snapshot={"filename": "a.pdf"},
    )
    db_session.add(citation)
    db_session.commit()
    return {
        "user_a": user_a,
        "user_b": user_b,
        "kb": kb,
        "doc": doc,
        "task": task,
        "attempt": attempt,
        "conv": conv,
        "msg": msg,
        "citation": citation,
    }


class TestKnowledgeBaseScope:
    def test_cross_user_knowledge_base_20002(self, db_session: Session, tenant_fixture) -> None:
        user_b = tenant_fixture["user_b"]
        kb = tenant_fixture["kb"]
        with pytest.raises(ApiError) as exc:
            require_knowledge_base(db_session, kb.id, user_b.id)
        assert exc.value.code == 20002
        assert exc.value.http_status == 404
        assert exc.value.message == _KB_NOT_FOUND_MSG

    def test_cross_user_knowledge_base_via_repository(
        self, db_session: Session, tenant_fixture
    ) -> None:
        repo = TenantScopedRepository(db_session, KnowledgeBase, not_found_code=20002)
        with pytest.raises(ApiError) as exc:
            repo.get_for_user(tenant_fixture["kb"].id, tenant_fixture["user_b"].id)
        assert exc.value.code == 20002


class TestOtherResourceScope:
    @pytest.mark.parametrize("resource", ["doc", "task", "attempt", "conv", "msg", "citation"])
    def test_cross_user_resources_20007(
        self, db_session: Session, tenant_fixture, resource: str
    ) -> None:
        user_b = tenant_fixture["user_b"]
        row = tenant_fixture[resource]
        repo = TenantScopedRepository(db_session, type(row), not_found_code=20007)
        with pytest.raises(ApiError) as exc:
            repo.get_for_user(row.id, user_b.id)
        assert exc.value.code == 20007
        assert exc.value.http_status == 404
        # 不泄露内容：与普通不存在使用完全相同的固定提示。
        assert exc.value.message == _NOT_FOUND_MSG

    def test_attempt_repository_scope(self, db_session: Session, tenant_fixture) -> None:
        repo = DocumentTaskAttemptRepository(db_session)
        with pytest.raises(ApiError) as exc:
            repo.get_for_user(tenant_fixture["attempt"].id, tenant_fixture["user_b"].id)
        assert exc.value.code == 20007
        assert exc.value.message == _NOT_FOUND_MSG

    def test_nonexistent_id_same_error_as_cross_user(
        self, db_session: Session, tenant_fixture
    ) -> None:
        # 随机不存在的 ID 与跨用户访问返回完全相同的错误（禁止全局探测）。
        repo = TenantScopedRepository(db_session, Document, not_found_code=20007)
        missing = uuid.uuid4()
        with pytest.raises(ApiError) as exc:
            repo.get_for_user(missing, tenant_fixture["user_a"].id)
        assert exc.value.code == 20007
        assert exc.value.message == _NOT_FOUND_MSG
