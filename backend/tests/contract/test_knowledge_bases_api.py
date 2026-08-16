"""知识库 API 契约测试（T025 / FR-003、FR-020）。

覆盖：创建/查询/更新/空知识库同步删除、``page/page_size`` 分页与越界拒绝、跨租户
知识库 ``20002/404`` 且无全局探测、``delete_failed`` 禁止 PATCH 返回 ``20008/409``、
``deleting`` 从普通读取隐藏。需要真实 Redis 与测试数据库。
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.enums import DocumentStatus, FileType, KnowledgeBaseStatus
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User

pytestmark = pytest.mark.contract

_KB_NOT_FOUND_MSG = "请求的知识库不存在"
_KB_UUID = "00000000-0000-4000-8000-000000000010"


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


class TestCreate:
    def test_create_knowledge_base(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "kb-owner@example.com")
        resp = client.post(
            "/v1/knowledge-bases",
            json={"name": "我的知识库", "description": "说明"},
            headers=_headers(tokens),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["name"] == "我的知识库"
        assert data["description"] == "说明"
        assert data["status"] == "active"
        assert data["delete_error_code"] is None
        assert data["allowed_actions"] == ["delete"]
        assert data["created_at"] is not None

    def test_create_without_description(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "kb-min@example.com")
        resp = client.post("/v1/knowledge-bases", json={"name": "x"}, headers=_headers(tokens))
        assert resp.status_code == 201
        assert resp.json()["data"]["description"] is None


class TestList:
    def test_pagination_defaults_and_page_size_cap(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kb-list@example.com")
        headers = _headers(tokens)
        for i in range(3):
            client.post("/v1/knowledge-bases", json={"name": f"kb-{i}"}, headers=headers)
        resp = client.get("/v1/knowledge-bases", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert data["total"] == 3
        assert len(data["items"]) == 3
        # page_size 上限 100；越界拒绝 10003/400。
        resp = client.get("/v1/knowledge-bases?page_size=101", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003
        resp = client.get("/v1/knowledge-bases?page=0", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_deleting_knowledge_base_hidden_from_list(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kb-hide@example.com")
        headers = _headers(tokens)
        user = db_session.query(User).filter_by(email="kb-hide@example.com").one()
        active = KnowledgeBase(user_id=user.id, name="visible")
        deleting = KnowledgeBase(user_id=user.id, name="hidden")
        db_session.add_all([active, deleting])
        db_session.flush()
        deleting.status = KnowledgeBaseStatus.DELETING
        db_session.commit()

        resp = client.get("/v1/knowledge-bases", headers=headers)
        names = [item["name"] for item in resp.json()["data"]["items"]]
        assert "visible" in names
        assert "hidden" not in names


class TestGet:
    def test_get_own_knowledge_base(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kb-get@example.com")
        user = db_session.query(User).filter_by(email="kb-get@example.com").one()
        kb = KnowledgeBase(user_id=user.id, name="mine")
        db_session.add(kb)
        db_session.commit()
        resp = client.get(f"/v1/knowledge-bases/{kb.id}", headers=_headers(tokens))
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "mine"

    def test_cross_user_get_20002_without_probe(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "kb-a@example.com")
        tokens_b = _register(client, "kb-b@example.com")
        user_a = db_session.query(User).filter_by(email="kb-a@example.com").one()
        kb = KnowledgeBase(user_id=user_a.id, name="a-only")
        db_session.add(kb)
        db_session.commit()

        resp = client.get(f"/v1/knowledge-bases/{kb.id}", headers=_headers(tokens_b))
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 20002
        assert body["msg"] == _KB_NOT_FOUND_MSG
        # 与随机不存在 ID 返回完全相同的错误（禁止全局探测）。
        ghost = client.get(f"/v1/knowledge-bases/{uuid.uuid4()}", headers=_headers(tokens_b))
        assert ghost.status_code == 404
        assert ghost.json()["code"] == 20002
        assert ghost.json()["msg"] == body["msg"]

    def test_deleting_knowledge_base_get_20002(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kb-hide2@example.com")
        user = db_session.query(User).filter_by(email="kb-hide2@example.com").one()
        kb = KnowledgeBase(user_id=user.id, name="x")
        db_session.add(kb)
        db_session.flush()
        kb.status = KnowledgeBaseStatus.DELETING
        db_session.commit()
        resp = client.get(f"/v1/knowledge-bases/{kb.id}", headers=_headers(tokens))
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002


class TestUpdate:
    def test_update_own_knowledge_base(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kb-upd@example.com")
        user = db_session.query(User).filter_by(email="kb-upd@example.com").one()
        kb = KnowledgeBase(user_id=user.id, name="old")
        db_session.add(kb)
        db_session.commit()
        resp = client.patch(
            f"/v1/knowledge-bases/{kb.id}",
            json={"name": "new", "description": "d"},
            headers=_headers(tokens),
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["name"] == "new"
        assert data["description"] == "d"

    def test_delete_failed_knowledge_base_patch_20008(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kb-fail@example.com")
        user = db_session.query(User).filter_by(email="kb-fail@example.com").one()
        kb = KnowledgeBase(user_id=user.id, name="x")
        db_session.add(kb)
        db_session.flush()
        kb.status = KnowledgeBaseStatus.DELETE_FAILED
        kb.delete_error_code = 20015
        db_session.commit()
        resp = client.patch(
            f"/v1/knowledge-bases/{kb.id}", json={"name": "y"}, headers=_headers(tokens)
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == 20008


class TestDelete:
    def test_delete_empty_knowledge_base(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kb-del@example.com")
        user = db_session.query(User).filter_by(email="kb-del@example.com").one()
        kb = KnowledgeBase(user_id=user.id, name="empty")
        db_session.add(kb)
        db_session.commit()
        resp = client.delete(f"/v1/knowledge-bases/{kb.id}", headers=_headers(tokens))
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        # 删除后再次访问 404。
        resp = client.delete(f"/v1/knowledge-bases/{kb.id}", headers=_headers(tokens))
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002

    def test_delete_non_empty_orchestrates_and_hides(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "kb-full@example.com")
        headers = _headers(tokens)
        user = db_session.query(User).filter_by(email="kb-full@example.com").one()
        kb = KnowledgeBase(user_id=user.id, name="full")
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
        db_session.commit()
        # T081 非空知识库删除编排：置 deleting、编排资料删除，提交后立即隐藏。
        resp = client.delete(f"/v1/knowledge-bases/{kb.id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        db_session.expire_all()
        kb = db_session.get(KnowledgeBase, kb.id)
        assert kb is not None
        assert kb.status == KnowledgeBaseStatus.DELETING
        doc = db_session.get(Document, doc.id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETING
        # 删除中的知识库及子资源对普通读取隐藏。
        resp = client.get(f"/v1/knowledge-bases/{kb.id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        resp = client.get(f"/v1/knowledge-bases/{kb.id}/documents", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        # 重复 DELETE 幂等成功且不创建新任务。
        tasks_before = db_session.query(DocumentTask).count()
        resp = client.delete(f"/v1/knowledge-bases/{kb.id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.query(DocumentTask).count() == tasks_before

    def test_cross_user_delete_20002(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "kb-da@example.com")
        tokens_b = _register(client, "kb-db@example.com")
        user_a = db_session.query(User).filter_by(email="kb-da@example.com").one()
        kb = KnowledgeBase(user_id=user_a.id, name="a")
        db_session.add(kb)
        db_session.commit()
        resp = client.delete(f"/v1/knowledge-bases/{kb.id}", headers=_headers(tokens_b))
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
