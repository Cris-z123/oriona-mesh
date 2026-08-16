"""知识库删除编排集成测试（T079 / FR-003、FR-011、FR-020）。

覆盖：非空知识库 DELETE 置 ``deleting`` 并编排全部资料的有界删除；删除中知识库及
子资源对普通读取隐藏；重复 ``deleting`` DELETE 幂等不建任务；任一子资料清理耗尽后
维护扫描器收敛为 ``delete_failed/20015`` 最小墓碑（仅所有者可见、不含名称/描述/
子资源）；再次 DELETE 才转回 ``deleting`` 并仅为失败子资料新建轮次；全部子资料
``deleted`` 后物理删除并级联对话/消息/引用，之后再次 DELETE 404。
需要真实 PostgreSQL 与 Redis。
"""

import io
import uuid

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message, MessageCitation
from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.enums import (
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    KnowledgeBaseStatus,
    MessageFinishReason,
    MessageRole,
    MessageStatus,
)
from app.models.knowledge_base import KnowledgeBase
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.workers.document_delete_cleanup import process_delete_cleanup
from app.workers.task_recovery import scan_knowledge_base_deletions

pytestmark = pytest.mark.integration

_KB_NOT_FOUND_MSG = "请求的知识库不存在"
_RESOURCE_NOT_FOUND_MSG = "请求的资源不存在"


def _uf(name: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name)


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
    resp = client.post(
        "/v1/knowledge-bases",
        json={"name": name, "description": "说明"},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


def _seed_document(
    db_session: Session,
    storage: FileStorage,
    dispatch,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    filename: str = "doc.txt",
) -> uuid.UUID:
    service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
    outcome = service.upload(user_id, kb_id, [_uf(filename, b"hello kb deletion")])
    return uuid.UUID(outcome.items[0]["id"])


def _cleanup_task(
    db_session: Session, doc_id: uuid.UUID, delete_cycle: int | None = None
) -> DocumentTask:
    query = db_session.query(DocumentTask).filter_by(
        document_id=doc_id, task_type=DocumentTaskType.DELETE_CLEANUP
    )
    if delete_cycle is not None:
        query = query.filter_by(delete_cycle=delete_cycle)
    else:
        query = query.order_by(DocumentTask.delete_cycle.desc())
    task = query.first()
    assert task is not None
    return task


def _seed_conversation(db_session: Session, user_id: uuid.UUID, kb_id: uuid.UUID) -> None:
    """知识库下的对话/消息/引用；物理删除知识库时应全部级联。"""
    conversation = Conversation(user_id=user_id, knowledge_base_id=kb_id, title="c")
    db_session.add(conversation)
    db_session.flush()
    message = Message(
        user_id=user_id,
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETED,
        finish_reason=MessageFinishReason.STOP,
        content="回答",
    )
    db_session.add(message)
    db_session.flush()
    db_session.add(
        MessageCitation(
            message_id=message.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=None,
            chunk_id=None,
            document_version=1,
            rank=1,
            score=0.5,
            chunk_snapshot={"filename": "doc.txt", "file_type": "txt", "content": "预览"},
        )
    )
    db_session.commit()


class TestKnowledgeBaseDeleteOrchestration:
    def test_non_empty_kb_delete_hides_and_orchestrates_documents(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        dispatch, _ = dispatch_calls
        tokens = _register(client, "kbdel-owner@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        from app.models.user import User

        user = db_session.query(User).filter_by(email="kbdel-owner@example.com").one()
        doc_a = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id), "a.txt")
        doc_b = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id), "b.md")
        _seed_conversation(db_session, user.id, uuid.UUID(kb_id))

        # 非空知识库 DELETE：置 deleting 并编排全部资料（立即对普通读取隐藏）。
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        db_session.expire_all()
        kb = db_session.get(KnowledgeBase, uuid.UUID(kb_id))
        assert kb is not None
        assert kb.status == KnowledgeBaseStatus.DELETING
        for doc_id in (doc_a, doc_b):
            doc = db_session.get(Document, doc_id)
            assert doc is not None
            assert doc.status == DocumentStatus.DELETING
            assert doc.delete_cycle == 1
            assert doc.current_task_type == DocumentTaskType.DELETE_CLEANUP
            cleanup = _cleanup_task(db_session, doc_id)
            assert cleanup.status == DocumentTaskStatus.QUEUED
            assert cleanup.delete_cycle == 1
        # 未开始任务已取消（不再投递普通阶段）。
        parse_tasks = (
            db_session.query(DocumentTask)
            .filter(DocumentTask.task_type == DocumentTaskType.PARSE)
            .all()
        )
        assert all(t.status == DocumentTaskStatus.CANCELLED for t in parse_tasks)

        # 知识库及子资源立即不可见。
        resp = client.get(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        resp = client.get("/v1/knowledge-bases", headers=headers)
        names = [item["name"] for item in resp.json()["data"]["items"]]
        assert "kb" not in names

        # 命中 deleting：重复 DELETE 幂等成功且不创建任务。
        tasks_before = db_session.query(DocumentTask).count()
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()
        kb = db_session.get(KnowledgeBase, uuid.UUID(kb_id))
        assert kb is not None
        assert kb.status == KnowledgeBaseStatus.DELETING
        assert db_session.query(DocumentTask).count() == tasks_before

        # 全部清理成功后：扫描器物理删除知识库并级联对话/消息/引用。
        for doc_id in (doc_a, doc_b):
            cleanup = _cleanup_task(db_session, doc_id)
            process_delete_cleanup(
                db_session,
                task_id=cleanup.id,
                user_id=user.id,
                knowledge_base_id=uuid.UUID(kb_id),
                document_id=doc_id,
                document_version=1,
                file_storage=storage,
                dispatch=dispatch,
            )
            db_session.expire_all()
        scan_knowledge_base_deletions(db_session)
        db_session.expire_all()
        assert db_session.get(KnowledgeBase, uuid.UUID(kb_id)) is None
        assert db_session.query(Conversation).count() == 0
        assert db_session.query(Message).count() == 0
        assert db_session.query(MessageCitation).count() == 0
        # 物理删除完成后再次 DELETE 404。
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002

    def test_delete_failed_tombstone_retry_only_failed_subdocs(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        dispatch, _ = dispatch_calls
        tokens = _register(client, "kbretry-owner@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        from app.models.user import User

        user = db_session.query(User).filter_by(email="kbretry-owner@example.com").one()
        doc_a = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id), "a.txt")
        doc_b = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id), "b.md")

        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()

        # 子资料 A 清理成功；子资料 B 清理重试耗尽 → 20015。
        cleanup_a = _cleanup_task(db_session, doc_a)
        process_delete_cleanup(
            db_session,
            task_id=cleanup_a.id,
            user_id=user.id,
            knowledge_base_id=uuid.UUID(kb_id),
            document_id=doc_a,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.expire_all()

        class FailingStorage(FileStorage):
            def delete_object(self, object_key: str) -> None:
                raise OSError("disk gone")

        broken = FailingStorage(storage.storage)
        cleanup_b = _cleanup_task(db_session, doc_b)
        for _round in range(4):  # 初次 + 3 次重试全部失败
            process_delete_cleanup(
                db_session,
                task_id=cleanup_b.id,
                user_id=user.id,
                knowledge_base_id=uuid.UUID(kb_id),
                document_id=doc_b,
                document_version=1,
                file_storage=broken,
                dispatch=dispatch,
            )
            db_session.expire_all()
            cleanup_b = _cleanup_task(db_session, doc_b)
        doc_b_row = db_session.get(Document, doc_b)
        assert doc_b_row is not None
        assert doc_b_row.status == DocumentStatus.FAILED
        assert doc_b_row.error_code == 20015

        # 维护扫描器：任一子资料 20015 → 知识库收敛为 delete_failed 最小墓碑。
        assert scan_knowledge_base_deletions(db_session) == 1
        db_session.expire_all()
        kb = db_session.get(KnowledgeBase, uuid.UUID(kb_id))
        assert kb is not None
        assert kb.status == KnowledgeBaseStatus.DELETE_FAILED
        assert kb.delete_error_code == 20015

        # 墓碑仅所属用户可见：不含名称/描述/子资源，仅 retry_delete。
        resp = client.get("/v1/knowledge-bases", headers=headers)
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        tomb = items[0]
        assert tomb["id"] == kb_id
        assert tomb["name"] is None
        assert tomb["description"] is None
        assert tomb["status"] == "delete_failed"
        assert tomb["delete_error_code"] == 20015
        assert tomb["allowed_actions"] == ["retry_delete"]
        resp = client.get(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.json()["data"]["allowed_actions"] == ["retry_delete"]
        # 子资源仍不可见；墓碑不可编辑。
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        resp = client.patch(f"/v1/knowledge-bases/{kb_id}", json={"name": "x"}, headers=headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == 20008

        # 墓碑仅所属用户可见：他人列表无该项、详情 20002。
        other_tokens = _register(client, "kbretry-other@example.com")
        other_headers = _headers(other_tokens)
        assert client.get("/v1/knowledge-bases", headers=other_headers).json()["data"]["total"] == 0
        resp = client.get(f"/v1/knowledge-bases/{kb_id}", headers=other_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002

        # 再次 DELETE 才转回 deleting，并仅为失败子资料新建轮次。
        tasks_before = db_session.query(DocumentTask).count()
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()
        kb = db_session.get(KnowledgeBase, uuid.UUID(kb_id))
        assert kb is not None
        assert kb.status == KnowledgeBaseStatus.DELETING
        assert kb.delete_error_code is None
        # 仅失败子资料 B 新建轮次；A 已 deleted，保持原轮次。
        doc_a_row = db_session.get(Document, doc_a)
        assert doc_a_row is not None
        assert doc_a_row.status == DocumentStatus.DELETED
        assert doc_a_row.delete_cycle == 1
        doc_b_row = db_session.get(Document, doc_b)
        assert doc_b_row is not None
        assert doc_b_row.delete_cycle == 2
        retry_cleanup = _cleanup_task(db_session, doc_b)
        assert retry_cleanup.delete_cycle == 2
        assert retry_cleanup.status == DocumentTaskStatus.QUEUED
        assert db_session.query(DocumentTask).count() == tasks_before + 1  # 只为 B 新建 1 个
        # 旧清理任务历史保留（B 的 cycle-1 任务仍为 failed/20015）。
        old_cleanup = _cleanup_task(db_session, doc_b, delete_cycle=1)
        assert old_cleanup.status == DocumentTaskStatus.FAILED
        assert old_cleanup.error_code == 20015

        # 重试清理成功 → 全部子资料 deleted → 物理删除。
        process_delete_cleanup(
            db_session,
            task_id=retry_cleanup.id,
            user_id=user.id,
            knowledge_base_id=uuid.UUID(kb_id),
            document_id=doc_b,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.expire_all()
        scan_knowledge_base_deletions(db_session)
        db_session.expire_all()
        assert db_session.get(KnowledgeBase, uuid.UUID(kb_id)) is None
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002

    def test_scan_keeps_deleting_while_docs_still_cleaning(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        dispatch, _ = dispatch_calls
        tokens = _register(client, "kbscan-owner@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        from app.models.user import User

        user = db_session.query(User).filter_by(email="kbscan-owner@example.com").one()
        doc_id = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id))
        assert client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers).status_code == 200
        db_session.expire_all()

        # 子资料仍在清理（deleting/清理任务未完成）：扫描不收敛、不物理删除。
        assert scan_knowledge_base_deletions(db_session) == 0
        db_session.expire_all()
        kb = db_session.get(KnowledgeBase, uuid.UUID(kb_id))
        assert kb is not None
        assert kb.status == KnowledgeBaseStatus.DELETING
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETING

        # 清理完成后才物理删除。
        cleanup = _cleanup_task(db_session, doc_id)
        process_delete_cleanup(
            db_session,
            task_id=cleanup.id,
            user_id=user.id,
            knowledge_base_id=uuid.UUID(kb_id),
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.expire_all()
        assert scan_knowledge_base_deletions(db_session) == 1
        db_session.expire_all()
        assert db_session.get(KnowledgeBase, uuid.UUID(kb_id)) is None

    def test_empty_knowledge_base_delete_immediate_404_after(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kbempty-owner@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 200
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
