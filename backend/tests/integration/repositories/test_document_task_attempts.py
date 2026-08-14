"""任务尝试仓储集成测试（T021 / data-model.md attempt 规则）。

覆盖：attempt 创建事务锁定父任务并复制/校验租户边界；边界不一致由仓储拒绝，且
数据库五列复合外键作为最后一道一致性约束直接拒绝不匹配写入；同一任务最多一个
running attempt；读取固定过滤当前用户。
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    FileType,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.repositories.document_task_attempts import DocumentTaskAttemptRepository

pytestmark = pytest.mark.integration


@pytest.fixture()
def attempt_fixture(db_session: Session):
    user = User(email="owner@example.com", password_hash="h")
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db_session.add(kb)
    db_session.flush()
    doc = Document(
        user_id=user.id,
        knowledge_base_id=kb.id,
        filename="a.pdf",
        file_type=FileType.PDF,
        file_size=10,
        storage_path="o/a",
        upload_batch_id=uuid.uuid4(),
        content_hash="c",
        status=DocumentStatus.QUEUED,
    )
    db_session.add(doc)
    db_session.flush()
    task = DocumentTask(
        user_id=user.id,
        knowledge_base_id=kb.id,
        document_id=doc.id,
        document_version=1,
        task_type=DocumentTaskType.PARSE,
        status=DocumentTaskStatus.QUEUED,
        idempotency_key="parse:doc:v1",
    )
    db_session.add(task)
    db_session.commit()
    return {"user": user, "kb": kb, "doc": doc, "task": task}


class TestCreateAttempt:
    def test_creates_attempt_with_task_boundaries(
        self, db_session: Session, attempt_fixture
    ) -> None:
        task = attempt_fixture["task"]
        repo = DocumentTaskAttemptRepository(db_session)
        attempt = repo.create_for_task(
            task_id=task.id,
            user_id=task.user_id,
            knowledge_base_id=task.knowledge_base_id,
            document_id=task.document_id,
            document_version=task.document_version,
            worker_name="worker-1",
            started_at=datetime.now(UTC),
        )
        db_session.commit()
        assert attempt.attempt_no == 1
        assert attempt.status.value == "running"
        assert attempt.started_at is not None
        assert attempt.worker_name == "worker-1"
        assert attempt.finished_at is None
        assert attempt.duration_ms is None
        # 冗余边界与父任务完全一致。
        assert attempt.user_id == task.user_id
        assert attempt.knowledge_base_id == task.knowledge_base_id
        assert attempt.document_id == task.document_id
        assert attempt.document_version == task.document_version

    def test_mismatched_boundary_rejected_by_repository(
        self, db_session: Session, attempt_fixture
    ) -> None:
        task = attempt_fixture["task"]
        repo = DocumentTaskAttemptRepository(db_session)
        with pytest.raises(ApiError) as exc:
            repo.create_for_task(
                task_id=task.id,
                user_id=uuid.uuid4(),  # 任一边界不匹配即拒绝
                knowledge_base_id=task.knowledge_base_id,
                document_id=task.document_id,
                document_version=task.document_version,
                worker_name="w",
                started_at=datetime.now(UTC),
            )
        assert exc.value.code == 20008
        db_session.rollback()

    def test_attempt_no_increments_within_task(self, db_session: Session, attempt_fixture) -> None:
        task = attempt_fixture["task"]
        repo = DocumentTaskAttemptRepository(db_session)
        first = repo.create_for_task(
            task_id=task.id,
            user_id=task.user_id,
            knowledge_base_id=task.knowledge_base_id,
            document_id=task.document_id,
            document_version=task.document_version,
            worker_name="w",
            started_at=datetime.now(UTC),
        )
        db_session.commit()
        # 关闭第一个 attempt 后再创建第二个。
        first.status = DocumentAttemptStatus.SUCCEEDED
        first.finished_at = datetime.now(UTC)
        db_session.commit()
        second = repo.create_for_task(
            task_id=task.id,
            user_id=task.user_id,
            knowledge_base_id=task.knowledge_base_id,
            document_id=task.document_id,
            document_version=task.document_version,
            worker_name="w",
            started_at=datetime.now(UTC),
        )
        db_session.commit()
        assert second.attempt_no == first.attempt_no + 1


class TestDatabaseCompositeForeignKey:
    def test_mismatched_boundary_rejected_by_composite_fk(
        self, db_session: Session, attempt_fixture
    ) -> None:
        # 绕过仓储直接写入不匹配边界的 attempt：数据库复合外键必须拒绝。
        task = attempt_fixture["task"]
        bad = DocumentTaskAttempt(
            task_id=task.id,
            user_id=uuid.uuid4(),  # 与父任务不一致
            knowledge_base_id=task.knowledge_base_id,
            document_id=task.document_id,
            document_version=task.document_version,
            attempt_no=1,
            worker_name="w",
            status="running",
            started_at=datetime.now(UTC),
        )
        db_session.add(bad)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_second_open_attempt_rejected_by_partial_unique(
        self, db_session: Session, attempt_fixture
    ) -> None:
        task = attempt_fixture["task"]
        repo = DocumentTaskAttemptRepository(db_session)
        repo.create_for_task(
            task_id=task.id,
            user_id=task.user_id,
            knowledge_base_id=task.knowledge_base_id,
            document_id=task.document_id,
            document_version=task.document_version,
            worker_name="w",
            started_at=datetime.now(UTC),
        )
        db_session.commit()
        # 同一任务第二个 running attempt：部分唯一索引必须拒绝。
        second = DocumentTaskAttempt(
            task_id=task.id,
            user_id=task.user_id,
            knowledge_base_id=task.knowledge_base_id,
            document_id=task.document_id,
            document_version=task.document_version,
            attempt_no=2,
            worker_name="w",
            status="running",
            started_at=datetime.now(UTC),
        )
        db_session.add(second)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


class TestScopedReads:
    def test_reads_filter_current_user(self, db_session: Session, attempt_fixture) -> None:
        task = attempt_fixture["task"]
        other = User(email="other@example.com", password_hash="h")
        db_session.add(other)
        db_session.commit()
        repo = DocumentTaskAttemptRepository(db_session)
        attempt = repo.create_for_task(
            task_id=task.id,
            user_id=task.user_id,
            knowledge_base_id=task.knowledge_base_id,
            document_id=task.document_id,
            document_version=task.document_version,
            worker_name="w",
            started_at=datetime.now(UTC),
        )
        db_session.commit()
        # 其他用户无法读取该 attempt（20007，不全局探测）。
        with pytest.raises(ApiError) as exc:
            repo.get_for_user(attempt.id, other.id)
        assert exc.value.code == 20007
        # 其他用户视角下任务无未结束 attempt。
        assert repo.get_open_for_task(task.id, other.id) is None
