"""资料/任务状态 API 契约测试（T078 / FR-005、FR-007、FR-008a、FR-010、FR-020）。

覆盖：
- 资料公开状态枚举（pending/queued/processing/completed/failed）与过滤；
  内部 ``deleting/deleted`` 传入过滤返回 ``10003/400`` 且不能暴露隐藏资料；
- 任务阶段（parse/chunk/embed/finalize/cleanup/delete_cleanup）与任务/尝试状态枚举；
- 完整尝试记录 DTO：worker、非空 ``started_at``、可空 ``finished_at/error_message/duration_ms``；
- 持久化 ``error_code``（20001/20010~20015/50000）与固定安全提示映射；
- 失败原因仅对所有者可见（跨用户统一 ``20007/404``）；
- 失败后无重处理操作：``allowed_actions`` 仅 ``delete``/``retry_delete``，
  ``failed/delete_cleanup/20015`` 仅为 ``retry_delete``。
需要真实 Redis 与测试数据库。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
)
from app.models.user import User

pytestmark = pytest.mark.contract

_KB_NOT_FOUND_MSG = "请求的知识库不存在"
_RESOURCE_NOT_FOUND_MSG = "请求的资源不存在"
_ASYNC_MESSAGES = {
    20001: "资料解析失败，请删除后重新上传",
    20010: "资料内容为空，请删除后重新上传",
    20011: "文件保存失败，请删除后重新上传",
    20012: "资料向量化失败，请删除后重新上传",
    20013: "资料处理结果不一致，请删除后重新上传",
    20014: "资料处理失败，请删除后重新上传",
    20015: "资料删除未完成，请重试删除",
    50000: "系统繁忙，请稍后再试",
}
# openapi.yaml Document.error_code 允许的持久化失败码。
_ASYNC_ERROR_CODES = (20001, 20010, 20011, 20012, 20013, 20014, 20015, 50000)


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


def _create_kb(client: TestClient, headers: dict, name: str = "kb") -> str:
    resp = client.post("/v1/knowledge-bases", json={"name": name}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


def _upload(
    client: TestClient, kb_id: str, files: list[tuple[str, bytes]], headers: dict, **kwargs
):
    multipart = [("files", (name, content, "application/octet-stream")) for name, content in files]
    return client.post(
        f"/v1/knowledge-bases/{kb_id}/documents", files=multipart, headers=headers, **kwargs
    )


# ---------------------------------------------------------------------------
# 直接播种工具（绕过 API 构造指定终态）
# ---------------------------------------------------------------------------


def _seed_document(
    db_session: Session,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    *,
    status: DocumentStatus,
    filename: str = "doc.txt",
    current_task_type: DocumentTaskType | None = None,
    error_code: int | None = None,
    delete_cycle: int = 0,
) -> Document:
    doc = Document(
        user_id=user_id,
        knowledge_base_id=kb_id,
        filename=filename,
        file_type="txt",
        file_size=10,
        storage_path=f"obj/seed/{uuid.uuid4()}",
        upload_batch_id=uuid.uuid4(),
        content_hash="c",
        status=status,
        current_task_type=current_task_type,
        error_code=error_code,
        error_message=_ASYNC_MESSAGES.get(error_code) if error_code else None,
        delete_cycle=delete_cycle,
        processing_finished_at=datetime.now(UTC) if status == DocumentStatus.FAILED else None,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def _seed_task(
    db_session: Session,
    doc: Document,
    *,
    task_type: DocumentTaskType,
    delete_cycle: int,
    status: DocumentTaskStatus,
    error_code: int | None = None,
    retry_count: int = 0,
) -> DocumentTask:
    task = DocumentTask(
        user_id=doc.user_id,
        knowledge_base_id=doc.knowledge_base_id,
        document_id=doc.id,
        document_version=doc.version,
        task_type=task_type,
        delete_cycle=delete_cycle,
        status=status,
        retry_count=retry_count,
        max_retries=3,
        error_code=error_code,
        error_message=_ASYNC_MESSAGES.get(error_code) if error_code else None,
        idempotency_key=f"seed-{uuid.uuid4()}",
        queued_at=datetime.now(UTC) - timedelta(minutes=5)
        if status != DocumentTaskStatus.PENDING
        else None,
        started_at=datetime.now(UTC) - timedelta(minutes=4),
        finished_at=datetime.now(UTC)
        if status
        in (DocumentTaskStatus.FAILED, DocumentTaskStatus.SUCCEEDED, DocumentTaskStatus.CANCELLED)
        else None,
    )
    db_session.add(task)
    db_session.flush()
    return task


def _seed_attempt(
    db_session: Session,
    task: DocumentTask,
    *,
    attempt_no: int,
    status: DocumentAttemptStatus,
    started_at: datetime | None = None,
    error_message: str | None = None,
) -> DocumentTaskAttempt:
    started_at = started_at or (datetime.now(UTC) - timedelta(minutes=4))
    attempt = DocumentTaskAttempt(
        task_id=task.id,
        user_id=task.user_id,
        knowledge_base_id=task.knowledge_base_id,
        document_id=task.document_id,
        document_version=task.document_version,
        attempt_no=attempt_no,
        worker_name="orionamesh-seed-worker",
        status=status,
        started_at=started_at,
        finished_at=datetime.now(UTC) if status != DocumentAttemptStatus.RUNNING else None,
        error_message=error_message,
        duration_ms=123 if status != DocumentAttemptStatus.RUNNING else None,
    )
    db_session.add(attempt)
    db_session.flush()
    return attempt


class TestPublicStatusFilter:
    """公开资料状态过滤仅允许 pending/queued/processing/completed/failed。"""

    def test_all_public_status_values_accepted(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-filter@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="st-filter@example.com").one()
        for status in (
            DocumentStatus.PENDING,
            DocumentStatus.QUEUED,
            DocumentStatus.PROCESSING,
            DocumentStatus.COMPLETED,
            DocumentStatus.FAILED,
        ):
            _seed_document(db_session, user.id, uuid.UUID(kb_id), status=status)
        db_session.commit()
        for value in ("pending", "queued", "processing", "completed", "failed"):
            resp = client.get(
                f"/v1/knowledge-bases/{kb_id}/documents?status={value}", headers=headers
            )
            assert resp.status_code == 200, value
            assert resp.json()["data"]["total"] == 1, value

    def test_internal_status_deleting_rejected_10003(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-hide1@example.com")
        kb_id = _create_kb(client, _headers(tokens))
        resp = client.get(
            f"/v1/knowledge-bases/{kb_id}/documents?status=deleting", headers=_headers(tokens)
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_internal_status_deleted_rejected_10003(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-hide2@example.com")
        kb_id = _create_kb(client, _headers(tokens))
        resp = client.get(
            f"/v1/knowledge-bases/{kb_id}/documents?status=deleted", headers=_headers(tokens)
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_unknown_status_rejected_10003(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "st-bad@example.com")
        kb_id = _create_kb(client, _headers(tokens))
        resp = client.get(
            f"/v1/knowledge-bases/{kb_id}/documents?status=wat", headers=_headers(tokens)
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_hidden_documents_never_listed(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-hidden@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="st-hidden@example.com").one()
        kb = uuid.UUID(kb_id)
        _seed_document(db_session, user.id, kb, status=DocumentStatus.COMPLETED)
        _seed_document(db_session, user.id, kb, status=DocumentStatus.DELETING)
        _seed_document(db_session, user.id, kb, status=DocumentStatus.DELETED)
        db_session.commit()

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)
        data = resp.json()["data"]
        assert data["total"] == 1  # 只暴露 completed；deleting/deleted 隐藏
        assert all(item["status"] == "completed" for item in data["items"])


class TestDocumentDetailDto:
    """资料详情 DTO：公开状态、持久化失败码、安全提示与 allowed_actions。"""

    def test_queued_document_dto_full_shape(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-shape@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("doc.txt", b"hello")], headers)
        doc_id = resp.json()["data"]["documents"][0]["id"]

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == doc_id  # 详情必须返回同一资料
        assert data["knowledge_base_id"] == kb_id
        assert data["filename"] == "doc.txt"
        assert data["file_type"] == "txt"
        assert data["status"] == "queued"
        assert data["version"] == 1
        assert data["current_task_type"] == "parse"
        assert data["retry_count"] == 0
        assert data["delete_cycle"] == 0
        assert data["chunk_count"] == 0
        assert data["error_code"] is None
        assert data["error_message"] is None
        assert data["processing_started_at"] is None
        assert data["processing_finished_at"] is None
        assert data["allowed_actions"] == ["delete"]

    @pytest.mark.parametrize("error_code", _ASYNC_ERROR_CODES)
    def test_persisted_error_code_and_fixed_message(
        self,
        client: TestClient,
        db_session: Session,
        clean_rate_limit_keys,
        error_code: int,
    ) -> None:
        tokens = _register(client, f"st-err{error_code}@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email=f"st-err{error_code}@example.com").one()
        doc = _seed_document(
            db_session,
            user.id,
            uuid.UUID(kb_id),
            status=DocumentStatus.FAILED,
            current_task_type=DocumentTaskType.PARSE,
            error_code=error_code,
        )
        db_session.commit()

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}", headers=headers)
        assert resp.status_code == 200  # 详情仍返回 HTTP 200（FR-034）
        data = resp.json()["data"]
        assert data["status"] == "failed"
        assert data["error_code"] == error_code
        assert data["error_message"] == _ASYNC_MESSAGES[error_code]
        # 失败后无重处理操作：仅允许删除。
        assert data["allowed_actions"] == ["delete"]

    def test_delete_cleanup_failed_tombstone_only_retry_delete(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        """failed/delete_cleanup/20015 映射为“删除未完成”墓碑，而非普通失败。"""
        tokens = _register(client, "st-tomb@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="st-tomb@example.com").one()
        doc = _seed_document(
            db_session,
            user.id,
            uuid.UUID(kb_id),
            status=DocumentStatus.FAILED,
            current_task_type=DocumentTaskType.DELETE_CLEANUP,
            error_code=20015,
            delete_cycle=1,
        )
        db_session.commit()

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "failed"
        assert data["current_task_type"] == "delete_cleanup"
        assert data["error_code"] == 20015
        assert data["error_message"] == "资料删除未完成，请重试删除"
        assert data["allowed_actions"] == ["retry_delete"]
        assert "delete" not in data["allowed_actions"]
        assert all(a in ("delete", "retry_delete") for a in data["allowed_actions"])

    def test_completed_document_allowed_delete_only(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-done@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="st-done@example.com").one()
        doc = _seed_document(db_session, user.id, uuid.UUID(kb_id), status=DocumentStatus.COMPLETED)
        db_session.commit()
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}", headers=headers)
        assert resp.json()["data"]["allowed_actions"] == ["delete"]

    def test_hidden_documents_detail_20007(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-ghost@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="st-ghost@example.com").one()
        for status in (DocumentStatus.DELETING, DocumentStatus.DELETED):
            doc = _seed_document(db_session, user.id, uuid.UUID(kb_id), status=status)
            db_session.commit()
            resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}", headers=headers)
            assert resp.status_code == 404
            assert resp.json()["code"] == 20007
            assert resp.json()["msg"] == _RESOURCE_NOT_FOUND_MSG

    def test_failure_reason_only_visible_to_owner(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        """失败原因（error_code/error_message）仅对所有者可见。

        跨用户访问知识库先按契约映射 ``20002/404``（FR-020，知识库为第一范围）；
        本人知识库下不存在的资料才映射 ``20007/404``；均不泄露资源存在性。
        """
        tokens_a = _register(client, "st-owner@example.com")
        kb_id = _create_kb(client, _headers(tokens_a), name="owner-kb")
        user_a = db_session.query(User).filter_by(email="st-owner@example.com").one()
        doc = _seed_document(
            db_session,
            user_a.id,
            uuid.UUID(kb_id),
            status=DocumentStatus.FAILED,
            current_task_type=DocumentTaskType.PARSE,
            error_code=20001,
        )
        db_session.commit()
        tokens_b = _register(client, "st-other@example.com")

        resp = client.get(
            f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}", headers=_headers(tokens_b)
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 20002  # 知识库不在当前用户范围内
        assert "error_code" not in body
        assert "error_message" not in body
        # 不泄露资源存在性：与随机 ID 返回完全相同的错误。
        ghost = client.get(
            f"/v1/knowledge-bases/{kb_id}/documents/{uuid.uuid4()}", headers=_headers(tokens_b)
        )
        assert ghost.status_code == 404
        assert ghost.json()["code"] == 20002
        assert ghost.json()["msg"] == body["msg"]
        # 本人知识库下不存在的资料才映射 20007/404（资源层面不可见）。
        own_ghost = client.get(
            f"/v1/knowledge-bases/{kb_id}/documents/{uuid.uuid4()}", headers=_headers(tokens_a)
        )
        assert own_ghost.status_code == 404
        assert own_ghost.json()["code"] == 20007
        assert own_ghost.json()["msg"] == _RESOURCE_NOT_FOUND_MSG


class TestTaskDto:
    """任务列表 DTO：阶段枚举、delete_cycle 与完整尝试记录。"""

    def _kb_owned_doc(
        self, client: TestClient, db_session: Session, email: str
    ) -> tuple[str, str, uuid.UUID]:
        tokens = _register(client, email)
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email=email).one()
        return kb_id, headers["Authorization"].split()[-1], user.id

    def test_queued_parse_task_dto(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-task0@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("doc.txt", b"hello")], headers)
        doc_id = resp.json()["data"]["documents"][0]["id"]

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}/tasks", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        task = data["items"][0]
        assert task["document_id"] == doc_id
        assert task["document_version"] == 1
        assert task["task_type"] == "parse"
        assert task["delete_cycle"] == 0  # 非 delete_cleanup 任务固定为 0
        assert task["status"] == "queued"
        assert task["retry_count"] == 0
        assert task["max_retries"] == 3
        assert task["error_code"] is None
        assert task["error_message"] is None
        assert task["attempts"] == []  # 尚未开始执行

    def test_failed_task_with_complete_attempt_dto(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "st-task1@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="st-task1@example.com").one()
        doc = _seed_document(
            db_session,
            user.id,
            uuid.UUID(kb_id),
            status=DocumentStatus.FAILED,
            current_task_type=DocumentTaskType.PARSE,
            error_code=20001,
        )
        task = _seed_task(
            db_session,
            doc,
            task_type=DocumentTaskType.PARSE,
            delete_cycle=0,
            status=DocumentTaskStatus.FAILED,
            error_code=20001,
            retry_count=3,
        )
        _seed_attempt(
            db_session,
            task,
            attempt_no=1,
            status=DocumentAttemptStatus.FAILED,
            error_message=_ASYNC_MESSAGES[20001],
        )
        _seed_attempt(
            db_session,
            task,
            attempt_no=2,
            status=DocumentAttemptStatus.FAILED,
            error_message=_ASYNC_MESSAGES[20001],
        )
        db_session.commit()

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}/tasks", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        task_dto = data["items"][0]
        assert task_dto["status"] == "failed"
        assert task_dto["retry_count"] == 3
        assert task_dto["error_code"] == 20001
        assert task_dto["error_message"] == _ASYNC_MESSAGES[20001]
        assert task_dto["started_at"] is not None
        assert task_dto["finished_at"] is not None
        # 完整 Attempt DTO：worker、非空 started_at、可空结束/错误/耗时。
        attempts = task_dto["attempts"]
        assert [a["attempt_no"] for a in attempts] == [1, 2]
        for a in attempts:
            assert a["worker_name"]  # worker 标识
            assert uuid.UUID(a["id"]) is not None
            assert a["started_at"] is not None  # attempt 创建时即写入
            assert a["finished_at"] is not None
            assert a["error_message"] is not None
            assert a["duration_ms"] is not None and a["duration_ms"] >= 0
            assert a["status"] == "failed"

    def test_running_attempt_nullable_fields(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        """未结束 attempt：finished_at/error_message/duration_ms 为 null。"""
        tokens = _register(client, "st-task2@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="st-task2@example.com").one()
        doc = _seed_document(
            db_session,
            user.id,
            uuid.UUID(kb_id),
            status=DocumentStatus.PROCESSING,
            current_task_type=DocumentTaskType.CHUNK,
        )
        task = _seed_task(
            db_session,
            doc,
            task_type=DocumentTaskType.CHUNK,
            delete_cycle=0,
            status=DocumentTaskStatus.RUNNING,
        )
        _seed_attempt(db_session, task, attempt_no=1, status=DocumentAttemptStatus.RUNNING)
        db_session.commit()

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}/tasks", headers=headers)
        attempt = resp.json()["data"]["items"][0]["attempts"][0]
        assert attempt["status"] == "running"
        assert attempt["started_at"] is not None
        assert attempt["finished_at"] is None
        assert attempt["error_message"] is None
        assert attempt["duration_ms"] is None

    def test_delete_cleanup_task_dto_with_cycle(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        """delete_cleanup 任务：task_type/delete_cycle 与 20015 持久化失败码。"""
        tokens = _register(client, "st-task3@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="st-task3@example.com").one()
        doc = _seed_document(
            db_session,
            user.id,
            uuid.UUID(kb_id),
            status=DocumentStatus.FAILED,
            current_task_type=DocumentTaskType.DELETE_CLEANUP,
            error_code=20015,
            delete_cycle=2,
        )
        task = _seed_task(
            db_session,
            doc,
            task_type=DocumentTaskType.DELETE_CLEANUP,
            delete_cycle=2,
            status=DocumentTaskStatus.FAILED,
            error_code=20015,
            retry_count=3,
        )
        _seed_attempt(
            db_session,
            task,
            attempt_no=1,
            status=DocumentAttemptStatus.FAILED,
            error_message=_ASYNC_MESSAGES[20015],
        )
        db_session.commit()

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}/tasks", headers=headers)
        assert resp.status_code == 200
        task_dto = resp.json()["data"]["items"][0]
        assert task_dto["task_type"] == "delete_cleanup"
        assert task_dto["delete_cycle"] == 2
        assert task_dto["status"] == "failed"
        assert task_dto["error_code"] == 20015
        assert task_dto["error_message"] == "资料删除未完成，请重试删除"

    def test_cross_user_tasks_20002(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens_a = _register(client, "st-task-a@example.com")
        kb_id = _create_kb(client, _headers(tokens_a), name="a")
        user_a = db_session.query(User).filter_by(email="st-task-a@example.com").one()
        doc = _seed_document(
            db_session, user_a.id, uuid.UUID(kb_id), status=DocumentStatus.COMPLETED
        )
        _seed_task(
            db_session,
            doc,
            task_type=DocumentTaskType.FINALIZE,
            delete_cycle=0,
            status=DocumentTaskStatus.SUCCEEDED,
        )
        db_session.commit()
        tokens_b = _register(client, "st-task-b@example.com")

        # 任务记录与资料同属知识库子资源：知识库不在当前用户范围内 → 20002/404。
        resp = client.get(
            f"/v1/knowledge-bases/{kb_id}/documents/{doc.id}/tasks", headers=_headers(tokens_b)
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
