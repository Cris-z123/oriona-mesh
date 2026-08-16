"""上传批次协调/补偿与资料访问集成测试（T038 / FR-004、FR-006、FR-020、FR-033）。

覆盖：数据库失败清临时对象并 50000/500；提交后转正前 ``pending`` 任务不可执行；
全部转正后整批资料/任务/幂等快照原子 ``queued`` 并投递 parse；任一转正失败三者
``failed/20011``、对象全清且零投递；``202`` 每项只能为 queued 或 failed/20011；
跨用户资料统一 ``20007/404`` 且无全局探测；详情 HTTP 200 返回持久化异步失败码；
公开状态过滤排除内部 deleting/deleted。需要真实 PostgreSQL 与 Redis。
"""

import io
import uuid
from pathlib import Path

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.infrastructure.storage.local import LocalStorage
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import DocumentStatus, DocumentTaskStatus
from app.models.upload_request import DocumentUploadRequest
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage

pytestmark = pytest.mark.integration

_KB_NOT_FOUND_MSG = "请求的知识库不存在"
_RESOURCE_NOT_FOUND_MSG = "请求的资源不存在"
_PARSE_FAILED_MSG = "资料解析失败，请删除后重新上传"


# ---------------------------------------------------------------------------
# 共享夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch_fake():
    """记录投递调用的假 dispatch：(name, args)。"""
    calls: list[tuple[str, tuple]] = []

    def fake(name: str, args: tuple) -> None:
        calls.append((name, args))

    return fake, calls


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    return FileStorage(LocalStorage(tmp_path / "store"))


def _uf(name: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name)


# ---------------------------------------------------------------------------
# 服务层：批次协调与补偿
# ---------------------------------------------------------------------------


class TestBatchCoordination:
    def test_upload_single_file_queued_and_dispatched(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_fake,
        kb_and_user,
    ) -> None:
        dispatch, calls = dispatch_fake
        user_id, kb_id = kb_and_user
        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
        outcome = service.upload(
            user_id, kb_id, [_uf("a.pdf", b"%PDF-1.4")], idempotency_key="up-key-0001"
        )

        assert [item["status"] for item in outcome.items] == ["queued"]
        assert outcome.items[0]["current_task_type"] == "parse"
        # 资料/任务/幂等记录均原子 queued。
        doc = db_session.query(Document).one()
        assert doc.status == DocumentStatus.QUEUED
        task = db_session.query(DocumentTask).one()
        assert task.status == DocumentTaskStatus.QUEUED
        assert task.task_type.value == "parse"
        request = db_session.query(DocumentUploadRequest).one()
        assert request.status.value == "accepted"
        # 仅投递 parse，且以任务 ID 为参数。
        assert len(calls) == 1
        assert calls[0][0] == "orionamesh.document_parse"
        # 正式对象已存在，临时对象已转正。
        assert storage.has_final(doc.upload_batch_id, doc.id)
        assert not storage.has_temp(doc.upload_batch_id, doc.id)

    def test_upload_multi_file_same_batch_queued(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_fake,
        kb_and_user,
    ) -> None:
        dispatch, calls = dispatch_fake
        user_id, kb_id = kb_and_user
        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
        outcome = service.upload(
            user_id,
            kb_id,
            [_uf("a.pdf", b"%pdf"), _uf("b.md", b"# t"), _uf("c.txt", b"t")],
        )
        assert {item["status"] for item in outcome.items} == {"queued"}
        assert len(db_session.query(Document).all()) == 3
        assert len(db_session.query(DocumentTask).all()) == 3
        assert len(calls) == 3
        batches = {doc.upload_batch_id for doc in db_session.query(Document).all()}
        assert len(batches) == 1

    def test_database_failure_cleans_temp_and_returns_50000(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_fake,
        kb_and_user,
        monkeypatch,
    ) -> None:
        dispatch, calls = dispatch_fake
        user_id, kb_id = kb_and_user
        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)

        def boom(*_args, **_kwargs):
            raise RuntimeError("db commit failed")

        monkeypatch.setattr(service, "_persist_batch", boom)
        with pytest.raises(Exception) as exc_info:
            service.upload(user_id, kb_id, [_uf("a.pdf", b"%PDF")])
        # 服务层把持久化失败收敛为 50000/500（未分类内部错误）。
        assert getattr(exc_info.value, "code", None) == 50000
        # 无资料/任务/幂等记录，临时对象全部清理。
        assert db_session.query(Document).count() == 0
        assert db_session.query(DocumentTask).count() == 0
        assert db_session.query(DocumentUploadRequest).count() == 0
        assert list((storage.storage_root / "tmp").iterdir()) == []

    def test_pending_batch_not_executable_then_coordinate(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_fake,
        kb_and_user,
    ) -> None:
        """数据库提交至文件转正期间：pending 任务不可执行；协调后整批 queued。"""
        dispatch, calls = dispatch_fake
        user_id, kb_id = kb_and_user
        from app.services.upload_validation import validate_upload_batch

        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
        validated = validate_upload_batch([_uf("a.pdf", b"%PDF")])
        request = service._persist_batch(
            user_id, kb_id, validated, uuid.uuid4(), "pending-key-1"
        )
        doc = db_session.query(Document).one()
        task = db_session.query(DocumentTask).one()
        assert doc.status == DocumentStatus.PENDING
        assert task.status == DocumentTaskStatus.PENDING

        # pending 任务不可执行：直接调用 parse worker 不得创建 attempt。
        from app.workers.document_parse import process_parse

        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc.id,
            document_version=1,
        )
        db_session.commit()
        assert db_session.query(DocumentTaskAttempt).count() == 0
        assert db_session.query(DocumentTask).one().status == DocumentTaskStatus.PENDING

        # 协调后整批 queued 并投递。
        outcome = service.coordinate_batch(request.upload_batch_id, request)
        assert [item["status"] for item in outcome.items] == ["queued"]
        db_session.refresh(doc)
        db_session.refresh(task)
        assert doc.status == DocumentStatus.QUEUED
        assert task.status == DocumentTaskStatus.QUEUED
        assert db_session.query(DocumentUploadRequest).one().status.value == "accepted"
        assert len(calls) == 1
        assert storage.has_final(doc.upload_batch_id, doc.id)

    def test_promotion_failure_compensates_whole_batch(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_fake,
        kb_and_user,
        monkeypatch,
    ) -> None:
        dispatch, calls = dispatch_fake
        user_id, kb_id = kb_and_user
        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)

        def promote_boom(*_args, **_kwargs):
            raise OSError("rename failed")

        monkeypatch.setattr(storage, "promote_batch", promote_boom)
        outcome = service.upload(
            user_id, kb_id, [_uf("a.pdf", b"%PDF")], idempotency_key="up-key-0002"
        )

        # 202 返回 failed/20011 项（补偿结果，不是 HTTP 400）。
        assert [item["status"] for item in outcome.items] == ["failed"]
        assert outcome.items[0]["error_code"] == 20011
        assert outcome.items[0]["current_task_type"] == "parse"
        doc = db_session.query(Document).one()
        task = db_session.query(DocumentTask).one()
        request = db_session.query(DocumentUploadRequest).one()
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20011
        assert task.status == DocumentTaskStatus.FAILED
        assert request.status.value == "failed"
        # 零 parse 投递；临时与正式对象全部清理。
        assert calls == []
        assert not storage.has_final(doc.upload_batch_id, doc.id)
        assert not storage.has_temp(doc.upload_batch_id, doc.id)

    def test_202_items_only_queued_or_failed_20011(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_fake,
        kb_and_user,
    ) -> None:
        dispatch, _ = dispatch_fake
        user_id, kb_id = kb_and_user
        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
        outcome = service.upload(user_id, kb_id, [_uf("a.pdf", b"%PDF"), _uf("b.txt", b"x")])
        for item in outcome.items:
            if item["status"] == "queued":
                assert item["current_task_type"] == "parse"
                assert item["error_code"] is None
            elif item["status"] == "failed":
                assert item["error_code"] == 20011
            else:
                pytest.fail(f"unexpected 202 item status: {item['status']}")

    def test_upload_to_cross_user_kb_rejected(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_fake,
        kb_and_user,
        second_user,
    ) -> None:
        dispatch, _ = dispatch_fake
        user_id, kb_id = kb_and_user
        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
        with pytest.raises(Exception) as exc_info:
            service.upload(second_user, kb_id, [_uf("a.pdf", b"%PDF")])
        assert getattr(exc_info.value, "code", None) == 20002
        assert db_session.query(Document).count() == 0


# ---------------------------------------------------------------------------
# API 层：访问边界与详情语义
# ---------------------------------------------------------------------------


class TestAccessBoundaries:
    def test_cross_user_detail_no_probe(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        no_celery_dispatch,
        clean_rate_limit_keys,
    ) -> None:
        tokens_a, user_a, kb_a, doc_a = _seeded_document(client, db_session, storage, "acc-a")
        tokens_b = _register(client, "acc-b@example.com")
        headers_b = _headers(tokens_b)
        # 知识库不在用户 B 范围内：先按契约映射 20002/404（FR-020）。
        resp = client.get(
            f"/v1/knowledge-bases/{kb_a}/documents/{doc_a}", headers=headers_b
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 20002
        assert body["msg"] == _KB_NOT_FOUND_MSG
        # 与随机不存在资料返回完全相同的错误（禁止全局存在性探测）。
        ghost = client.get(
            f"/v1/knowledge-bases/{kb_a}/documents/{uuid.uuid4()}", headers=headers_b
        )
        assert ghost.status_code == 404
        assert ghost.json()["code"] == 20002
        assert ghost.json()["msg"] == body["msg"]
        # 跨用户列表同样不可见。
        listing = client.get(f"/v1/knowledge-bases/{kb_a}/documents", headers=headers_b)
        assert listing.status_code == 404
        assert listing.json()["code"] == 20002
        # 本人知识库下不存在的资料才映射 20007/404。
        own_ghost = client.get(
            f"/v1/knowledge-bases/{kb_a}/documents/{uuid.uuid4()}", headers=_headers(tokens_a)
        )
        assert own_ghost.status_code == 404
        assert own_ghost.json()["code"] == 20007

    def test_detail_http_200_with_persisted_async_failure(
        self, client: TestClient, db_session: Session, storage: FileStorage, no_celery_dispatch
    ) -> None:
        tokens_a, user_a, kb_a, doc_a = _seeded_document(client, db_session, storage, "acc-f")
        doc = db_session.get(Document, doc_a)
        doc.status = DocumentStatus.FAILED
        doc.error_code = 20001
        doc.error_message = _PARSE_FAILED_MSG
        db_session.commit()
        headers = _headers(tokens_a)
        resp = client.get(
            f"/v1/knowledge-bases/{kb_a}/documents/{doc_a}", headers=headers
        )
        # 异步失败不伪装成上传阶段的 HTTP 400：详情仍为 200 信封。
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["status"] == "failed"
        assert data["error_code"] == 20001
        assert data["error_message"] == _PARSE_FAILED_MSG
        assert data["allowed_actions"] == ["delete"]

    def test_list_hides_deleting_and_rejects_internal_status_filter(
        self, client: TestClient, db_session: Session, storage: FileStorage, no_celery_dispatch
    ) -> None:
        tokens_a, user_a, kb_a, doc_a = _seeded_document(client, db_session, storage, "acc-h")
        # 再上传一份并直接置为 deleting。
        service = DocumentService(
            db_session, file_storage=storage, dispatch=lambda *a, **k: None
        )
        outcome = service.upload(user_a, kb_a, [_uf("hidden.pdf", b"%PDF")])
        hidden_id = outcome.items[0]["id"]
        hidden = db_session.get(Document, uuid.UUID(hidden_id))
        hidden.status = DocumentStatus.DELETING
        db_session.commit()

        headers = _headers(tokens_a)
        resp = client.get(f"/v1/knowledge-bases/{kb_a}/documents", headers=headers)
        items = resp.json()["data"]["items"]
        visible_ids = {item["id"] for item in items}
        assert str(doc_a) in visible_ids
        assert hidden_id not in visible_ids
        # 内部隐藏状态不得作为公开过滤条件。
        resp = client.get(
            f"/v1/knowledge-bases/{kb_a}/documents?status=deleting", headers=headers
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003
        # 公开状态过滤可用。
        resp = client.get(
            f"/v1/knowledge-bases/{kb_a}/documents?status=queued", headers=headers
        )
        assert resp.status_code == 200
        assert all(item["status"] == "queued" for item in resp.json()["data"]["items"])

    def test_cross_user_task_list_20002(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        no_celery_dispatch,
        clean_rate_limit_keys,
    ) -> None:
        tokens_a, user_a, kb_a, doc_a = _seeded_document(client, db_session, storage, "acc-t")
        tokens_b = _register(client, "acc-t2@example.com")
        resp = client.get(
            f"/v1/knowledge-bases/{kb_a}/documents/{doc_a}/tasks", headers=_headers(tokens_b)
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _seeded_document(
    client, db_session, storage, email: str
) -> tuple[dict, uuid.UUID, uuid.UUID, str]:
    """注册用户、创建知识库并上传一份有效资料；返回 (tokens, user_id, kb_id, doc_id)。"""
    from app.models.user import User

    tokens = _register(client, f"{email}@example.com")
    headers = _headers(tokens)
    resp = client.post("/v1/knowledge-bases", json={"name": "kb"}, headers=headers)
    kb_id = uuid.UUID(resp.json()["data"]["id"])
    user = db_session.query(User).filter_by(email=f"{email}@example.com").one()
    service = DocumentService(db_session, file_storage=storage, dispatch=lambda *a, **k: None)
    outcome = service.upload(user.id, kb_id, [_uf("seed.pdf", b"%PDF-1.4")])
    return tokens, user.id, kb_id, outcome.items[0]["id"]


def _register(client: TestClient, email: str) -> dict:
    assert (
        client.post("/v1/users", json={"email": email, "password": "password123"}).status_code
        == 201
    )
    return client.post(
        "/v1/auth/sessions", json={"email": email, "password": "password123"}
    ).json()["data"]


def _headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def kb_and_user(db_session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """直接创建用户与知识库（跳过 API），供服务层测试使用。"""
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User

    user = User(
        email="kb-owner@example.com",
        password_hash="x" * 60,
        display_name=None,
    )
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db_session.add(kb)
    db_session.commit()
    return user.id, kb.id


@pytest.fixture
def second_user(db_session: Session) -> uuid.UUID:
    from app.models.user import User

    user = User(email="other@example.com", password_hash="x" * 60, display_name=None)
    db_session.add(user)
    db_session.commit()
    return user.id


@pytest.fixture
def no_celery_dispatch(monkeypatch):
    """客户端测试：路由内 DocumentService 使用假 dispatch，不触碰真实 Celery。"""
    calls: list[tuple[str, tuple]] = []

    def fake(name: str, args: tuple) -> None:
        calls.append((name, args))

    monkeypatch.setattr("app.services.document_service._default_dispatch", fake)
    return calls
