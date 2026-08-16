"""上传请求幂等集成测试（T040 / FR-031、data-model.md 上传重放）。

覆盖：相同内容无键创建独立资料；同键重放命中已收敛请求复用首次结果且零重复资源；
同键不同请求（指纹不同）冲突 ``20008/409``；未超时 coordinating 重放 ``20008/409``
且零副作用；超过协调窗口后重放或扫描器按批次锁定接管；成功后快照为 queued、
补偿后快照为 failed/20011；幂等记录默认 24 小时保留并由维护扫描器过期清理。
需要真实 PostgreSQL。
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.infrastructure.storage.local import LocalStorage
from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.enums import DocumentStatus, DocumentTaskStatus
from app.models.upload_request import DocumentUploadRequest
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.workers.task_recovery import cleanup_expired_upload_requests

pytestmark = pytest.mark.integration

_CONFLICT_CODE = 20008
_KEY = "upload-key-0001"


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
def service(db_session: Session, storage: FileStorage, dispatch_calls) -> DocumentService:
    dispatch, calls = dispatch_calls
    svc = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
    svc._dispatch_calls = calls  # type: ignore[attr-defined]
    return svc


@pytest.fixture
def user_and_kb(db_session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User

    user = User(email="idem-owner@example.com", password_hash="x" * 60)
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db_session.add(kb)
    db_session.commit()
    return user.id, kb.id


def _files(count: int = 1) -> list:
    import io

    from fastapi import UploadFile

    return [
        UploadFile(file=io.BytesIO(b"%PDF-1.4 content"), filename=f"f{i}.pdf") for i in range(count)
    ]


class TestIdempotency:
    def test_same_content_without_key_creates_independent_documents(
        self, db_session: Session, service: DocumentService, user_and_kb
    ) -> None:
        user_id, kb_id = user_and_kb
        first = service.upload(user_id, kb_id, _files())
        second = service.upload(user_id, kb_id, _files())
        assert first.items[0]["id"] != second.items[0]["id"]
        assert db_session.query(Document).count() == 2
        assert db_session.query(DocumentTask).count() == 2

    def test_replay_same_key_returns_first_result_without_duplicates(
        self, db_session: Session, service: DocumentService, user_and_kb
    ) -> None:
        user_id, kb_id = user_and_kb
        first = service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        replay = service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        assert replay.replay is True
        assert [item["id"] for item in replay.items] == [item["id"] for item in first.items]
        assert db_session.query(Document).count() == 1
        assert db_session.query(DocumentTask).count() == 1
        # 重放不重复投递。
        assert len(service._dispatch_calls) == 1  # type: ignore[attr-defined]

    def test_same_key_different_request_conflict_20008(
        self, db_session: Session, service: DocumentService, user_and_kb
    ) -> None:
        import io

        from fastapi import UploadFile

        user_id, kb_id = user_and_kb
        service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        other = [UploadFile(file=io.BytesIO(b"different content"), filename="f0.pdf")]
        with pytest.raises(ApiError) as exc:
            service.upload(user_id, kb_id, other, idempotency_key=_KEY)
        assert exc.value.code == _CONFLICT_CODE
        assert db_session.query(Document).count() == 1

    def test_coordinating_replay_within_window_409_no_side_effects(
        self, db_session: Session, service: DocumentService, user_and_kb
    ) -> None:
        user_id, kb_id = user_and_kb
        from app.services.upload_validation import validate_upload_batch

        validated = validate_upload_batch(_files())
        request = service._persist_batch(user_id, kb_id, validated, uuid.uuid4(), _KEY)
        assert request is not None
        assert request.status.value == "coordinating"
        with pytest.raises(ApiError) as exc:
            service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        assert exc.value.code == _CONFLICT_CODE
        # 零副作用：仍是初始的 1 份资料/任务，幂等记录未被改写。
        assert db_session.query(Document).count() == 1
        assert db_session.query(DocumentTask).count() == 1
        fresh = db_session.get(DocumentUploadRequest, request.id)
        assert fresh is not None
        assert fresh.status.value == "coordinating"
        assert len(service._dispatch_calls) == 0  # type: ignore[attr-defined]

    def test_expired_coordinating_taken_over_by_replay(
        self, db_session: Session, service: DocumentService, user_and_kb
    ) -> None:
        user_id, kb_id = user_and_kb
        from app.services.upload_validation import validate_upload_batch

        validated = validate_upload_batch(_files())
        request = service._persist_batch(user_id, kb_id, validated, uuid.uuid4(), _KEY)
        assert request is not None
        request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()
        outcome = service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        # 超时重放锁定批次并调用同一协调函数接管：整批 queued。
        assert [item["status"] for item in outcome.items] == ["queued"]
        fresh = db_session.get(DocumentUploadRequest, request.id)
        assert fresh is not None
        assert fresh.status.value == "accepted"
        doc = db_session.query(Document).one()
        assert doc.status == DocumentStatus.QUEUED
        assert db_session.query(DocumentTask).one().status == DocumentTaskStatus.QUEUED
        assert len(service._dispatch_calls) == 1  # type: ignore[attr-defined]

    def test_expired_coordinating_taken_over_by_scanner(
        self, db_session: Session, service: DocumentService, user_and_kb, storage, dispatch_calls
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        from app.services.upload_validation import validate_upload_batch

        validated = validate_upload_batch(_files())
        request = service._persist_batch(user_id, kb_id, validated, uuid.uuid4(), _KEY)
        assert request is not None
        request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        # 批次资料必须超过协调窗口（扫描器按 created_at 复查超时）。
        from app.models.document import Document

        for doc in db_session.query(Document).all():
            doc.created_at = datetime.now(UTC) - timedelta(seconds=301)
        db_session.commit()
        from app.workers.task_recovery import scan_upload_batches

        taken = scan_upload_batches(
            db_session, storage=storage, dispatch=dispatch, now=datetime.now(UTC)
        )
        assert taken == [request.upload_batch_id]
        doc = db_session.query(Document).one()
        assert doc.status == DocumentStatus.QUEUED
        persisted = db_session.get(DocumentUploadRequest, request.id)
        assert persisted is not None
        assert persisted.status.value == "accepted"
        assert len(calls) == 1

    def test_compensated_batch_snapshot_failed_20011_and_replay(
        self, db_session: Session, service: DocumentService, user_and_kb, monkeypatch
    ) -> None:
        user_id, kb_id = user_and_kb

        def promote_boom(*_args, **_kwargs):
            raise OSError("rename failed")

        monkeypatch.setattr(service.file_storage, "promote_batch", promote_boom)
        first = service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        assert first.items[0]["status"] == "failed"
        assert first.items[0]["error_code"] == 20011
        request = db_session.query(DocumentUploadRequest).one()
        assert request.status.value == "failed"
        # 补偿后快照可重放：返回首次 failed/20011 结果，不重复创建。
        replay = service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        assert replay.replay is True
        assert replay.items[0]["error_code"] == 20011
        assert db_session.query(Document).count() == 1

    def test_expired_idempotency_records_cleaned_by_maintenance(
        self, db_session: Session, service: DocumentService, user_and_kb
    ) -> None:
        user_id, kb_id = user_and_kb
        service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        fresh = db_session.query(DocumentUploadRequest).one()
        assert fresh.expires_at > datetime.now(UTC)
        fresh.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()
        removed = cleanup_expired_upload_requests(db_session, now=datetime.now(UTC))
        assert removed == 1
        assert db_session.query(DocumentUploadRequest).count() == 0
        # 未过期记录保留。
        service.upload(user_id, kb_id, _files(), idempotency_key=_KEY)
        assert db_session.query(DocumentUploadRequest).count() == 1
