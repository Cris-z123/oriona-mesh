"""任务恢复与投递失败集成测试（T043 / FR-007、FR-008、data-model.md 阶段切换与
删除边界）。

覆盖：Celery 投递失败后 queued 任务由扫描器幂等重投；后台任务初次
``attempt_no=1/retry_count=0``、``max_retries=3`` 时最多 4 个 attempt、达到预算
不再排队；attempt 完整 DTO 字段（worker、非空 started_at、可空结束/错误/耗时）；
``documents.retry_count`` 镜像当前任务并在阶段切换时重置；资料 deleting 后心跳
不得续租，租约到期后扫描器取消 attempt/task、释放名额并激活 ``delete_cleanup``。
需要真实 PostgreSQL。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.infrastructure.storage.local import LocalStorage
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
)
from app.models.processing_lease import DocumentProcessingLease
from app.repositories.processing_leases import ProcessingLeaseRepository
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.workers.document_parse import process_parse
from app.workers.task_recovery import redispatch_stuck_queued_tasks, scan_expired_leases

pytestmark = pytest.mark.integration

_DISPATCH_STALE_SECONDS = 60


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    return FileStorage(LocalStorage(tmp_path / "store"))


@pytest.fixture
def dispatch_calls():
    calls: list[tuple[str, tuple]] = []

    def fake(name: str, args: tuple) -> None:
        calls.append((name, args))

    return fake, calls


@pytest.fixture
def user_and_kb(db_session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User

    user = User(email="recover-owner@example.com", password_hash="x" * 60)
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db_session.add(kb)
    db_session.commit()
    return user.id, kb.id


def _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, text: str) -> uuid.UUID:
    import io

    from fastapi import UploadFile

    service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
    outcome = service.upload(
        user_id, kb_id, [UploadFile(file=io.BytesIO(text.encode()), filename="doc.txt")]
    )
    return uuid.UUID(outcome.items[0]["id"])


def _task(db_session, doc_id) -> DocumentTask:
    return db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="parse").one()


def _running_state(
    db_session: Session,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    *,
    lease_expired: bool,
    doc_status: DocumentStatus = DocumentStatus.PROCESSING,
    task_status: DocumentTaskStatus = DocumentTaskStatus.RUNNING,
    retry_count: int = 0,
) -> tuple[DocumentTask, DocumentTaskAttempt, DocumentProcessingLease]:
    doc = db_session.get(Document, doc_id)
    assert doc is not None
    task = _task(db_session, doc_id)
    doc.status = doc_status
    task.status = task_status
    task.retry_count = retry_count
    started = datetime.now(UTC) - timedelta(seconds=600)
    lease = DocumentProcessingLease(
        user_id=user_id,
        document_id=doc_id,
        task_id=task.id,
        acquired_at=started,
        heartbeat_at=started,
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=300)
            if lease_expired
            else datetime.now(UTC) + timedelta(seconds=300)
        ),
    )
    db_session.add(lease)
    db_session.flush()
    attempt = DocumentTaskAttempt(
        task_id=task.id,
        user_id=user_id,
        knowledge_base_id=kb_id,
        document_id=doc_id,
        document_version=1,
        attempt_no=1,
        worker_name="lost-worker",
        status=DocumentAttemptStatus.RUNNING,
        started_at=started,
    )
    db_session.add(attempt)
    db_session.commit()
    return task, attempt, lease


class TestQueuedRedispatch:
    def test_stuck_queued_task_redispatched_idempotently(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "stuck")
        calls.clear()  # 种子上传的投递不计入断言
        task = _task(db_session, doc_id)
        # 模拟投递失败：任务保持 queued 且超过重投阈值。
        task.queued_at = datetime.now(UTC) - timedelta(seconds=_DISPATCH_STALE_SECONDS + 10)
        db_session.commit()
        redispatched = redispatch_stuck_queued_tasks(
            db_session, dispatch=dispatch, now=datetime.now(UTC)
        )
        assert redispatched == 1
        assert calls == [("orionamesh.document_parse", (task.id,))]
        db_session.refresh(task)
        assert task.status == DocumentTaskStatus.QUEUED  # 真相不变，重复投递幂等

    def test_fresh_queued_task_not_redispatched(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "fresh")
        calls.clear()  # 种子上传的投递不计入断言
        redispatched = redispatch_stuck_queued_tasks(
            db_session, dispatch=dispatch, now=datetime.now(UTC)
        )
        assert redispatched == 0
        assert calls == []


class TestRetryBudget:
    def test_max_four_attempts_then_converge_20014(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
        monkeypatch,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "budget")
        calls.clear()  # 种子上传的投递不计入断言

        def always_fail(*_args, **_kwargs):
            raise RuntimeError("transient worker failure")

        monkeypatch.setattr("app.workers.document_parse.parse_safely", always_fail)
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        task = _task(db_session, doc_id)
        for _round in range(4):
            process_parse(
                db_session,
                task_id=task.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=1,
                file_storage=storage,
                dispatch=dispatch,
            )
            db_session.refresh(doc)
        # 初次 attempt_no=1/retry_count=0；最多 4 个 attempt；达到预算不再排队。
        attempts = (
            db_session.query(DocumentTaskAttempt)
            .filter_by(task_id=task.id)
            .order_by(DocumentTaskAttempt.attempt_no)
            .all()
        )
        assert [a.attempt_no for a in attempts] == [1, 2, 3, 4]  # noqa: E501
        assert all(a.status == DocumentAttemptStatus.FAILED for a in attempts)
        task = _task(db_session, doc_id)
        assert task.status == DocumentTaskStatus.FAILED
        assert task.retry_count == task.max_retries  # 3
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20014
        assert doc.error_message == "资料处理失败，请删除后重新上传"
        # 达到预算后不得再次排队：前 3 轮失败各重投一次，第 4 轮不再投递。
        assert calls == [("orionamesh.document_parse", (task.id,))] * 3
        # 完整 attempt DTO 字段（worker、非空 started_at、可空结束/错误/耗时）。
        from app.api.v1.schemas.documents import document_task_attempt_dto

        for a in attempts:
            dto = document_task_attempt_dto(a)
            assert dto["worker_name"]  # 真实 worker 名称（orionamesh-parse）
            assert dto["started_at"] is not None
            assert dto["finished_at"] is not None
            assert dto["error_message"] is not None
            assert dto["duration_ms"] is not None and dto["duration_ms"] >= 0

    def test_retry_count_mirrors_current_task_and_resets_on_stage_switch(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
        monkeypatch,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "mirror")
        calls.clear()  # 种子上传的投递不计入断言

        state = {"failures": 2}

        def flaky(*_args, **_kwargs):
            if state["failures"] > 0:
                state["failures"] -= 1
                raise RuntimeError("flaky worker")
            return _ok_parse_output()

        monkeypatch.setattr("app.workers.document_parse.parse_safely", flaky)
        doc = db_session.get(Document, doc_id)
        for _round in range(3):  # 失败 2 次后成功
            task = _task(db_session, doc_id)
            process_parse(
                db_session,
                task_id=task.id,
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc_id,
                document_version=1,
                file_storage=storage,
                dispatch=dispatch,
            )
            db_session.refresh(doc)
        # 失败期间 documents.retry_count 镜像当前任务。
        # （2 次失败后任务重试计数为 2，随后成功切换到 chunk 时归零。）
        chunk_task = (
            db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="chunk").one()
        )
        db_session.refresh(doc)
        assert doc is not None
        assert doc.status == DocumentStatus.PROCESSING
        assert doc.current_task_type == DocumentTaskType.CHUNK
        assert chunk_task.retry_count == 0
        assert doc.retry_count == 0
        attempts = (
            db_session.query(DocumentTaskAttempt).order_by(DocumentTaskAttempt.attempt_no).all()
        )
        assert [a.attempt_no for a in attempts] == [1, 2, 3]
        assert calls[-1] == ("orionamesh.document_chunk", (chunk_task.id,))


class TestCeleryWrapper:
    def test_task_wrapper_runs_with_task_id_only(
        self,
        db_session: Session,
        dispatch_calls,
        user_and_kb,
        monkeypatch,
    ) -> None:
        """C1 回归：dispatch 只发送 task_id，包装器须能从任务行加载租户边界。"""
        from app.services.file_storage import default_file_storage
        from app.workers.celery_app import celery_app

        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        # 对象写入默认持久卷根（包装器使用默认存储）。
        default_fs = default_file_storage()
        doc_id = _seed_queued_document(
            db_session, default_fs, dispatch, user_id, kb_id, "wrapper e2e"
        )
        task = _task(db_session, doc_id)
        # 注册的 Celery 任务签名只接受 task_id。
        import inspect

        wrapped = celery_app.tasks["orionamesh.document_parse"]
        params = inspect.signature(wrapped.run).parameters
        assert list(params) == ["task_id"]
        # 单参数执行：任务行提供全部租户边界，解析成功并激活 chunk。
        wrapped.run(str(task.id))
        db_session.refresh(task)
        assert task.status == DocumentTaskStatus.SUCCEEDED
        from app.models.enums import DocumentTaskType

        chunk_task = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type=DocumentTaskType.CHUNK)
            .one()
        )
        assert chunk_task.status == DocumentTaskStatus.QUEUED


class TestDeleteCoordination:
    def test_heartbeat_blocked_after_document_deleting(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "hb")
        _task, _attempt, lease = _running_state(
            db_session, user_id, kb_id, doc_id, lease_expired=False
        )
        repo = ProcessingLeaseRepository(db_session)
        assert lease.task_id is not None
        assert repo.heartbeat(lease.id, doc_id, lease.task_id, lease_seconds=300) is True
        hidden = db_session.get(Document, doc_id)
        assert hidden is not None
        hidden.status = DocumentStatus.DELETING
        db_session.commit()
        before = db_session.get(DocumentProcessingLease, lease.id)
        assert before is not None
        before_expires_at = before.expires_at
        assert repo.heartbeat(lease.id, doc_id, lease.task_id, lease_seconds=300) is False
        # 资料进入 deleting 后心跳不得续租：expires_at 不延长。
        after = db_session.get(DocumentProcessingLease, lease.id)
        assert after is not None
        assert after.expires_at == before_expires_at

    def test_expired_lease_on_deleting_document_cancels_and_activates_delete_cleanup(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id, "del-scan")
        calls.clear()  # 种子上传的投递不计入断言
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        task, attempt, lease = _running_state(
            db_session,
            user_id,
            kb_id,
            doc_id,
            lease_expired=True,
            doc_status=DocumentStatus.DELETING,
        )
        doc.delete_cycle = 1
        doc.current_task_type = DocumentTaskType.PARSE
        db_session.commit()

        recovered = scan_expired_leases(db_session, dispatch=dispatch, now=datetime.now(UTC))
        assert recovered == 1
        db_session.refresh(task)
        db_session.refresh(attempt)
        db_session.refresh(lease)
        db_session.refresh(doc)
        # attempt/task 置 cancelled、名额释放。
        assert attempt.status == DocumentAttemptStatus.CANCELLED
        assert task.status == DocumentTaskStatus.CANCELLED
        assert lease.released_at is not None
        # 激活 delete_cleanup 并投递。
        cleanup_task = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type=DocumentTaskType.DELETE_CLEANUP)
            .one()
        )
        assert cleanup_task.status == DocumentTaskStatus.QUEUED
        assert cleanup_task.retry_count == 0
        assert cleanup_task.delete_cycle == 1
        assert doc.current_task_type == DocumentTaskType.DELETE_CLEANUP
        assert doc.retry_count == 0
        assert calls == [("orionamesh.document_delete_cleanup", (cleanup_task.id,))]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _ok_parse_output():
    from app.services.parsers.base import ParseOutput

    return ParseOutput(normalized_text="ok text", parser_name="txt", parser_version="1")
