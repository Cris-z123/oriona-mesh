"""资料与任务明确终态集成测试（T079 / FR-008、FR-008a、FR-011、FR-020）。

覆盖：处理失败与重试耗尽收敛为明确终态而不是无限 processing；资料
``deleting/deleted`` 从列表/详情立即隐藏（用户不会看到无限“处理中/删除中”）；
任务与 attempt 终态（succeeded/failed/cancelled）完整可解释；失败资料只提供
删除操作、不提供重处理。需要真实 PostgreSQL 与 Redis。
"""

import io
import uuid

import pytest
from fastapi import UploadFile
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
from app.services.document_deletion_service import DocumentDeletionService
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.workers.base import begin_attempt, converge_cancelled
from app.workers.document_delete_cleanup import process_delete_cleanup
from app.workers.document_parse import process_parse

pytestmark = pytest.mark.integration

_RESOURCE_NOT_FOUND_MSG = "请求的资源不存在"


def _uf(name: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name)


def _seed_queued_document(
    db_session: Session,
    storage: FileStorage,
    dispatch,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    text: str = "hello terminal state",
) -> uuid.UUID:
    service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
    outcome = service.upload(user_id, kb_id, [_uf("doc.txt", text.encode())])
    return uuid.UUID(outcome.items[0]["id"])


def _parse_task(db_session: Session, doc_id: uuid.UUID) -> DocumentTask:
    return db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="parse").one()


def _register(client: TestClient, email: str) -> tuple[dict, str]:
    assert (
        client.post("/v1/users", json={"email": email, "password": "password123"}).status_code
        == 201
    )
    tokens = client.post(
        "/v1/auth/sessions", json={"email": email, "password": "password123"}
    ).json()["data"]
    return tokens, tokens["access_token"]


class TestPipelineTerminalStates:
    """流水线驱动的资料/任务明确终态（不无限 processing）。"""

    def test_parse_failure_converges_failed_20001(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
        monkeypatch,
    ) -> None:
        from app.services.parsers.base import ParseError

        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id)
        task = _parse_task(db_session, doc_id)

        def broken(*_args, **_kwargs):
            # 与真实解析器一致：携带固定安全提示（quickstart/FR-034）。
            raise ParseError(code=20001, message="资料解析失败，请删除后重新上传")

        monkeypatch.setattr("app.workers.document_parse.parse_safely", broken)
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

        doc = db_session.get(Document, doc_id)
        assert doc is not None
        db_session.refresh(task)
        # 明确终态：资料/任务/attempt 全部收敛为 failed，不遗留 running/processing。
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20001
        assert doc.error_message == "资料解析失败，请删除后重新上传"
        assert doc.processing_finished_at is not None
        assert task.status == DocumentTaskStatus.FAILED
        assert task.error_code == 20001
        attempt = db_session.query(DocumentTaskAttempt).one()
        assert attempt.status == DocumentAttemptStatus.FAILED
        assert attempt.worker_name == "orionamesh-parse"
        assert attempt.started_at is not None
        assert attempt.finished_at is not None
        assert attempt.error_message is not None
        assert attempt.duration_ms is not None
        # 失败后只允许删除，无重处理操作。
        from app.api.v1.schemas.documents import document_dto

        assert document_dto(doc)["allowed_actions"] == ["delete"]

    def test_empty_document_converges_20010(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
        monkeypatch,
    ) -> None:
        from app.services.parsers.base import ParseError

        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id)
        task = _parse_task(db_session, doc_id)

        def empty(*_args, **_kwargs):
            raise ParseError(code=20010, message="资料内容为空，请删除后重新上传")

        monkeypatch.setattr("app.workers.document_parse.parse_safely", empty)
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
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20010
        assert doc.error_message == "资料内容为空，请删除后重新上传"

    def test_unclassified_failure_retries_exhaust_20014(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
        monkeypatch,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id)
        calls.clear()  # 种子上传的投递不计入断言

        def always_fail(*_args, **_kwargs):
            raise RuntimeError("transient worker failure")

        monkeypatch.setattr("app.workers.document_parse.parse_safely", always_fail)
        for _round in range(4):  # max_retries=3：初次 + 3 次重试
            task = _parse_task(db_session, doc_id)
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
            db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        task = _parse_task(db_session, doc_id)
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20014  # 无法归类的重试耗尽
        assert task.status == DocumentTaskStatus.FAILED
        assert task.retry_count == 3
        attempts = (
            db_session.query(DocumentTaskAttempt)
            .filter_by(task_id=task.id)
            .order_by(DocumentTaskAttempt.attempt_no)
            .all()
        )
        assert [a.attempt_no for a in attempts] == [1, 2, 3, 4]
        assert all(a.status == DocumentAttemptStatus.FAILED for a in attempts)
        assert calls == [("orionamesh.document_parse", (task.id,))] * 3  # 达到预算不再排队

    def test_parse_success_attempt_succeeded_next_stage_queued(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id)
        calls.clear()
        task = _parse_task(db_session, doc_id)
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
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.PROCESSING  # 仍在处理中但阶段明确
        assert doc.current_task_type == DocumentTaskType.CHUNK
        assert doc.retry_count == 0
        db_session.refresh(task)
        assert task.status == DocumentTaskStatus.SUCCEEDED
        attempt = db_session.query(DocumentTaskAttempt).one()
        assert attempt.status == DocumentAttemptStatus.SUCCEEDED
        chunk_task = (
            db_session.query(DocumentTask).filter_by(document_id=doc_id, task_type="chunk").one()
        )
        assert chunk_task.status == DocumentTaskStatus.QUEUED
        # 提交后才投递下一阶段任务（以新任务 ID 为参数）。
        assert calls == [("orionamesh.document_chunk", (chunk_task.id,))]


class TestCancelledTerminal:
    def test_converge_cancelled_closes_attempt_and_task(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_id = _seed_queued_document(db_session, storage, dispatch, user_id, kb_id)
        task = _parse_task(db_session, doc_id)
        task, attempt = begin_attempt(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            worker_name="orionamesh-parse",
        )
        converge_cancelled(db_session, attempt_id=attempt.id)
        db_session.refresh(attempt)
        db_session.refresh(task)
        assert attempt.status == DocumentAttemptStatus.CANCELLED
        assert attempt.finished_at is not None
        assert attempt.duration_ms is not None
        assert task.status == DocumentTaskStatus.CANCELLED
        assert task.finished_at is not None


class TestVisibilityTerminal:
    """deleting/deleted 不向用户展示无限处理中，失败墓碑只对所有者可见。"""

    def _owned_kb_doc(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch,
        email: str,
    ) -> tuple[str, str, uuid.UUID, uuid.UUID]:
        """注册用户（API）、创建知识库（API）、以服务层种入 queued 资料。"""
        tokens, token = _register(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post("/v1/knowledge-bases", json={"name": "kb"}, headers=headers)
        kb_id = resp.json()["data"]["id"]
        user = db_session.query(User).filter_by(email=email).one()
        doc_id = _seed_queued_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id))
        return headers["Authorization"], kb_id, user.id, doc_id

    def test_deleting_document_hidden_and_delete_idempotent(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        dispatch, calls = dispatch_calls
        auth, kb_id, user_id, doc_id = self._owned_kb_doc(
            client, db_session, storage, dispatch, "terminal-vis@example.com"
        )
        headers = {"Authorization": auth}
        DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch).delete(
            user_id, uuid.UUID(kb_id), doc_id
        )
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETING
        # 立即隐藏：列表与详情均不可见，不得展示为“处理中/删除中”。
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)
        assert resp.json()["data"]["total"] == 0
        # 重复 DELETE 幂等成功（20008/409 或 404 都不允许出现）。
        calls_before = len(calls)
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        assert len(calls) == calls_before  # 不创建重复任务

    def test_deleted_document_get_and_delete_404(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        dispatch, _ = dispatch_calls
        auth, kb_id, user_id, doc_id = self._owned_kb_doc(
            client, db_session, storage, dispatch, "terminal-gone@example.com"
        )
        headers = {"Authorization": auth}
        service = DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch)
        service.delete(user_id, uuid.UUID(kb_id), doc_id)
        db_session.expire_all()
        cleanup = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type="delete_cleanup")
            .one()
        )
        process_delete_cleanup(
            db_session,
            task_id=cleanup.id,
            user_id=user_id,
            knowledge_base_id=uuid.UUID(kb_id),
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETED  # 保留不可查询的墓碑

        for method in ("get", "delete"):
            resp = getattr(client, method)(
                f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers
            )
            assert resp.status_code == 404
            assert resp.json()["code"] == 20007
            assert resp.json()["msg"] == _RESOURCE_NOT_FOUND_MSG

    def test_delete_cleanup_failed_tombstone_terminal_only_for_owner(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        dispatch, _ = dispatch_calls
        auth, kb_id, user_id, doc_id = self._owned_kb_doc(
            client, db_session, storage, dispatch, "terminal-tomb@example.com"
        )
        headers = {"Authorization": auth}
        service = DocumentDeletionService(db_session, file_storage=storage, dispatch=dispatch)
        service.delete(user_id, uuid.UUID(kb_id), doc_id)
        db_session.expire_all()

        class FailingStorage(FileStorage):
            def delete_object(self, object_key: str) -> None:
                raise OSError("disk gone")

        cleanup = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type="delete_cleanup")
            .one()
        )
        for _round in range(4):  # 初次 + 3 次重试全部失败
            process_delete_cleanup(
                db_session,
                task_id=cleanup.id,
                user_id=user_id,
                knowledge_base_id=uuid.UUID(kb_id),
                document_id=doc_id,
                document_version=1,
                file_storage=FailingStorage(storage.storage),
                dispatch=dispatch,
            )
            db_session.expire_all()
            cleanup = (
                db_session.query(DocumentTask)
                .filter_by(document_id=doc_id, task_type="delete_cleanup")
                .one()
            )
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        # 收敛为删除未完成墓碑：不再停留在 deleting，也不伪装成普通失败。
        assert doc.status == DocumentStatus.FAILED
        assert doc.current_task_type == DocumentTaskType.DELETE_CLEANUP
        assert doc.error_code == 20015
        assert doc.error_message == "资料删除未完成，请重试删除"
        assert cleanup.status == DocumentTaskStatus.FAILED
        assert cleanup.retry_count == 3

        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "failed"
        assert data["current_task_type"] == "delete_cleanup"
        assert data["error_code"] == 20015
        assert data["allowed_actions"] == ["retry_delete"]
        # 墓碑对非所有者不可见（失败原因仅对所有者可见）。
        other_tokens, other_token = _register(client, "terminal-other@example.com")
        resp = client.get(
            f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002  # 知识库本身对他人不可见
