"""会话、消息与引用 API 契约测试（T060 / FR-013、FR-013a、FR-014、FR-016、FR-019、FR-020）。

覆盖：会话 CRUD/分页与页码越界、可空标题与 last_message_at、消息 DTO 与 assistant 状态/结束原因
严格配对（streaming/null、completed/stop|length、failed/error、cancelled/cancelled）、消息
before/limit 游标分页连续且无重复、统一 Citation DTO（live 强制两个 UUID、snapshot 强制两个 ID
为 null、定位/内容、rank 升序与页码分页）、知识库完成编排清理后的级联删除及跨用户会话/消息/引用
统一 ``20007/404``。需要真实 Redis 与测试数据库。
"""

import uuid
from itertools import count

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.chunk import Chunk
from app.models.conversation import Conversation, Message, MessageCitation
from app.models.document import Document
from app.models.enums import (
    DocumentStatus,
    FileType,
    MessageFinishReason,
    MessageRole,
    MessageStatus,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User

pytestmark = pytest.mark.contract

_RESOURCE_NOT_FOUND_MSG = "请求的资源不存在"
_KB_NOT_FOUND_MSG = "请求的知识库不存在"
_EMBEDDING = [0.1] + [0.0] * 1535


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema(test_engine):
    """本模块部分测试不注入 db_session；依赖本夹具以触发会话级 test_engine schema 创建。"""
    yield


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


_kb_name_counter = count(1)


def _create_kb(client: TestClient, headers: dict, name: str | None = None) -> uuid.UUID:
    # FR-003（阶段 12）：同一用户 active 名称规范化后唯一；默认名加序号避免同用户冲突 20016。
    resp = client.post(
        "/v1/knowledge-bases",
        json={"name": name or f"kb-{next(_kb_name_counter)}"},
        headers=headers,
    )
    assert resp.status_code == 201
    return uuid.UUID(resp.json()["data"]["id"])


def _conversation(
    db: Session,
    user_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    *,
    title: str | None = None,
) -> Conversation:
    conv = Conversation(user_id=user_id, knowledge_base_id=knowledge_base_id, title=title)
    db.add(conv)
    db.flush()
    return conv


class TestCreateConversation:
    def test_create_bound_to_knowledge_base(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-owner@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = client.post(
            "/v1/conversations", json={"knowledge_base_id": str(kb_id)}, headers=headers
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["knowledge_base_id"] == str(kb_id)
        assert data["title"] is None
        assert data["last_message_at"] is None
        assert data["created_at"] is not None

    def test_create_with_title(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "conv-title@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = client.post(
            "/v1/conversations",
            json={"knowledge_base_id": str(kb_id), "title": "我的对话"},
            headers=headers,
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["title"] == "我的对话"

    def test_create_without_knowledge_base_rejected(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-nokb@example.com")
        resp = client.post("/v1/conversations", json={}, headers=_headers(tokens))
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_create_with_unknown_knowledge_base_20002(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-unknown-kb@example.com")
        resp = client.post(
            "/v1/conversations",
            json={"knowledge_base_id": "00000000-0000-4000-8000-000000000010"},
            headers=_headers(tokens),
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 20002
        assert body["msg"] == _KB_NOT_FOUND_MSG

    def test_create_with_cross_user_knowledge_base_20002(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "conv-cross@example.com")
        other = User(email="conv-cross-other@example.com", password_hash="x" * 60)
        db_session.add(other)
        db_session.flush()
        kb = KnowledgeBase(user_id=other.id, name="other-kb")
        db_session.add(kb)
        db_session.commit()
        tokens = _register(client, "conv-cross-user@example.com")
        resp = client.post(
            "/v1/conversations",
            json={"knowledge_base_id": str(kb.id)},
            headers=_headers(tokens),
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        assert resp.json()["msg"] == _KB_NOT_FOUND_MSG

    def test_title_length_validated(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "conv-len@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = client.post(
            "/v1/conversations",
            json={"knowledge_base_id": str(kb_id), "title": "长" * 201},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003
        resp = client.post(
            "/v1/conversations",
            json={"knowledge_base_id": str(kb_id), "title": ""},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003


class TestListConversations:
    def test_filters_and_paginates_within_knowledge_base(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-kb-filter@example.com")
        headers = _headers(tokens)
        first_kb = _create_kb(client, headers)
        second_kb = _create_kb(client, headers)
        for index in range(2):
            assert (
                client.post(
                    "/v1/conversations",
                    json={"knowledge_base_id": str(first_kb), "title": f"first-{index}"},
                    headers=headers,
                ).status_code
                == 201
            )
        assert (
            client.post(
                "/v1/conversations",
                json={"knowledge_base_id": str(second_kb), "title": "second"},
                headers=headers,
            ).status_code
            == 201
        )

        response = client.get(
            f"/v1/conversations?knowledge_base_id={first_kb}&page=2&page_size=1",
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert len(data["items"]) == 1
        assert data["items"][0]["knowledge_base_id"] == str(first_kb)

    def test_pagination_defaults_and_cap(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "conv-list@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        for i in range(3):
            resp = client.post(
                "/v1/conversations",
                json={"knowledge_base_id": str(kb_id), "title": f"c-{i}"},
                headers=headers,
            )
            assert resp.status_code == 201
        resp = client.get("/v1/conversations", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert data["total"] == 3
        assert len(data["items"]) == 3
        resp = client.get("/v1/conversations?page_size=101", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003
        resp = client.get("/v1/conversations?page=0", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_cross_user_conversations_invisible(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-iso@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        client.post("/v1/conversations", json={"knowledge_base_id": str(kb_id)}, headers=headers)
        other = User(email="conv-iso-other@example.com", password_hash="x" * 60)
        db_session.add(other)
        db_session.flush()
        other_kb = KnowledgeBase(user_id=other.id, name="other")
        db_session.add(other_kb)
        db_session.flush()
        db_session.add(Conversation(user_id=other.id, knowledge_base_id=other_kb.id))
        db_session.commit()
        resp = client.get("/v1/conversations", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 1
        assert resp.json()["data"]["items"][0]["knowledge_base_id"] == str(kb_id)


class TestGetConversation:
    def test_get_own_conversation(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-get@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="conv-get@example.com").one()
        conv = _conversation(db_session, user.id, kb_id)
        db_session.commit()
        resp = client.get(f"/v1/conversations/{conv.id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == str(conv.id)
        assert data["knowledge_base_id"] == str(kb_id)

    def test_cross_user_get_20007(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "conv-get-owner@example.com")
        other = User(email="conv-get-owner2@example.com", password_hash="x" * 60)
        db_session.add(other)
        db_session.flush()
        other_kb = KnowledgeBase(user_id=other.id, name="other")
        db_session.add(other_kb)
        db_session.flush()
        conv = _conversation(db_session, other.id, other_kb.id)
        db_session.commit()
        tokens = _register(client, "conv-get-intruder@example.com")
        resp = client.get(f"/v1/conversations/{conv.id}", headers=_headers(tokens))
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007
        assert resp.json()["msg"] == _RESOURCE_NOT_FOUND_MSG


class TestRenameConversation:
    def test_rename_updates_title(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-rename@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="conv-rename@example.com").one()
        conv = _conversation(db_session, user.id, kb_id, title="old")
        db_session.commit()
        resp = client.patch(f"/v1/conversations/{conv.id}", json={"title": "new"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "new"

    def test_rename_empty_title_rejected(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-rename-empty@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="conv-rename-empty@example.com").one()
        conv = _conversation(db_session, user.id, kb_id)
        db_session.commit()
        resp = client.patch(f"/v1/conversations/{conv.id}", json={"title": ""}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_cross_user_rename_20007(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        other = User(email="conv-rename-owner@example.com", password_hash="x" * 60)
        db_session.add(other)
        db_session.flush()
        other_kb = KnowledgeBase(user_id=other.id, name="other")
        db_session.add(other_kb)
        db_session.flush()
        conv = _conversation(db_session, other.id, other_kb.id)
        db_session.commit()
        tokens = _register(client, "conv-rename-intruder@example.com")
        resp = client.patch(
            f"/v1/conversations/{conv.id}", json={"title": "x"}, headers=_headers(tokens)
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007


class TestDeleteConversation:
    def test_delete_conversation_cascades_messages_and_citations(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-del@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="conv-del@example.com").one()
        conv = _conversation(db_session, user.id, kb_id)
        db_session.flush()
        msg = Message(
            user_id=user.id,
            conversation_id=conv.id,
            role=MessageRole.USER,
            status=MessageStatus.COMPLETED,
            content="hi",
        )
        db_session.add(msg)
        db_session.flush()
        db_session.add(
            MessageCitation(
                message_id=msg.id,
                user_id=user.id,
                knowledge_base_id=kb_id,
                chunk_id=None,
                document_id=None,
                document_version=1,
                rank=1,
                score=0.9,
                chunk_snapshot={"filename": "a.txt", "file_type": "txt", "content": "x"},
            )
        )
        db_session.commit()
        resp = client.delete(f"/v1/conversations/{conv.id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        resp = client.get(f"/v1/conversations/{conv.id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007
        assert db_session.query(Message).filter_by(conversation_id=conv.id).count() == 0
        assert db_session.query(MessageCitation).filter_by(message_id=msg.id).count() == 0

    def test_cross_user_delete_20007(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        other = User(email="conv-del-owner@example.com", password_hash="x" * 60)
        db_session.add(other)
        db_session.flush()
        other_kb = KnowledgeBase(user_id=other.id, name="other")
        db_session.add(other_kb)
        db_session.flush()
        conv = _conversation(db_session, other.id, other_kb.id)
        db_session.commit()
        tokens = _register(client, "conv-del-intruder@example.com")
        resp = client.delete(f"/v1/conversations/{conv.id}", headers=_headers(tokens))
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007


class TestMessageList:
    def _user_with_kb_and_conv(
        self, client: TestClient, db_session: Session, email: str
    ) -> tuple[dict, dict, uuid.UUID, uuid.UUID]:
        tokens = _register(client, email)
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email=email).one()
        conv = _conversation(db_session, user.id, kb_id)
        db_session.commit()
        return tokens, headers, user.id, conv.id

    def test_message_dto_strict_status_finish_reason_pairs(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens, headers, user_id, conv_id = self._user_with_kb_and_conv(
            client, db_session, "conv-msg-dto@example.com"
        )
        db_session.add(
            Message(
                user_id=user_id,
                conversation_id=conv_id,
                role=MessageRole.USER,
                status=MessageStatus.COMPLETED,
                content="问题",
                rewritten_query="改写后的问题",
            )
        )
        pairs = [
            (MessageStatus.STREAMING, None),
            (MessageStatus.COMPLETED, MessageFinishReason.STOP),
            (MessageStatus.COMPLETED, MessageFinishReason.LENGTH),
            (MessageStatus.FAILED, MessageFinishReason.ERROR),
            (MessageStatus.CANCELLED, MessageFinishReason.CANCELLED),
        ]
        for status, reason in pairs:
            db_session.add(
                Message(
                    user_id=user_id,
                    conversation_id=conv_id,
                    role=MessageRole.ASSISTANT,
                    status=status,
                    finish_reason=reason,
                    content="回答",
                )
            )
        db_session.commit()

        resp = client.get(f"/v1/conversations/{conv_id}/messages", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        by_role_status = {(item["role"], item["status"]): item for item in data["items"]}
        # 用户消息固定 completed 且 finish_reason 为 null。
        user_msg = by_role_status[("user", "completed")]
        assert user_msg["finish_reason"] is None
        assert user_msg["conversation_id"] == str(conv_id)
        assert user_msg["rewritten_query"] == "改写后的问题"
        # assistant 严格配对。
        assert by_role_status[("assistant", "streaming")]["finish_reason"] is None
        assert by_role_status[("assistant", "completed")]["finish_reason"] in ("stop", "length")
        assert by_role_status[("assistant", "failed")]["finish_reason"] == "error"
        assert by_role_status[("assistant", "cancelled")]["finish_reason"] == "cancelled"
        for item in data["items"]:
            if item["role"] == "assistant":
                assert item["rewritten_query"] is None
            assert item["created_at"] is not None

    def test_message_cursor_pagination_continuous_no_duplicates(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens, headers, user_id, conv_id = self._user_with_kb_and_conv(
            client, db_session, "conv-cursor@example.com"
        )
        from datetime import UTC, datetime, timedelta

        base = datetime.now(UTC)
        for i in range(5):
            db_session.add(
                Message(
                    user_id=user_id,
                    conversation_id=conv_id,
                    role=MessageRole.USER,
                    status=MessageStatus.COMPLETED,
                    content=f"m{i}",
                    created_at=base + timedelta(minutes=i),
                )
            )
        db_session.commit()

        seen: list[str] = []
        before: str | None = None
        for _ in range(3):
            url = f"/v1/conversations/{conv_id}/messages?limit=2"
            if before is not None:
                url += f"&before={before}"
            resp = client.get(url, headers=headers)
            assert resp.status_code == 200
            data = resp.json()["data"]
            items = data["items"]
            assert len(items) <= 2
            ids = [item["id"] for item in items]
            assert not [i for i in ids if i in seen], "分页出现重复消息"
            seen.extend(ids)
            if not data["has_more"]:
                assert data["next_before"] is None
                break
            assert data["next_before"] is not None
            before = data["next_before"]
        assert len(seen) == 5
        assert sorted(seen) == sorted(
            [str(m.id) for m in db_session.query(Message).filter_by(conversation_id=conv_id)]
        )
        # limit 越界与非法 before 拒绝。
        resp = client.get(f"/v1/conversations/{conv_id}/messages?limit=101", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_cross_user_messages_20007(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        other = User(email="conv-msg-owner@example.com", password_hash="x" * 60)
        db_session.add(other)
        db_session.flush()
        other_kb = KnowledgeBase(user_id=other.id, name="other")
        db_session.add(other_kb)
        db_session.flush()
        conv = _conversation(db_session, other.id, other_kb.id)
        db_session.commit()
        tokens = _register(client, "conv-msg-intruder@example.com")
        resp = client.get(f"/v1/conversations/{conv.id}/messages", headers=_headers(tokens))
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007


class TestCitations:
    def _completed_document_with_chunk(
        self, db: Session, kb: KnowledgeBase, *, filename: str = "source.txt"
    ) -> tuple[Document, Chunk]:
        user = kb.user_id
        doc = Document(
            user_id=user,
            knowledge_base_id=kb.id,
            filename=filename,
            file_type=FileType.TXT,
            file_size=10,
            status=DocumentStatus.COMPLETED,
            version=1,
            storage_path="tmp/a.txt",
            upload_batch_id=uuid.uuid4(),
            content_hash="x" * 64,
            chunk_count=1,
        )
        db.add(doc)
        db.flush()
        chunk = Chunk(
            user_id=user,
            knowledge_base_id=kb.id,
            document_id=doc.id,
            document_version=1,
            seq=0,
            content="来源内容预览",
            embedding=_EMBEDDING,
            embedding_model="text-embedding-3-small",
            policy_version="v1",
            page=2,
            section="结论",
        )
        db.add(chunk)
        db.flush()
        return doc, chunk

    def test_live_citation_dto_requires_both_ids_and_location_fields(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-cite@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="conv-cite@example.com").one()
        kb = db_session.get(KnowledgeBase, kb_id)
        assert kb is not None
        doc, chunk = self._completed_document_with_chunk(db_session, kb)
        conv = _conversation(db_session, user.id, kb_id)
        db_session.flush()
        msg = Message(
            user_id=user.id,
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            finish_reason=MessageFinishReason.STOP,
            content="回答",
        )
        db_session.add(msg)
        db_session.flush()
        db_session.add(
            MessageCitation(
                message_id=msg.id,
                user_id=user.id,
                knowledge_base_id=kb_id,
                chunk_id=chunk.id,
                document_id=doc.id,
                document_version=1,
                rank=1,
                score=0.91,
                chunk_snapshot={"filename": "stale.txt", "file_type": "txt", "content": "旧"},
            )
        )
        db_session.commit()

        resp = client.get(
            f"/v1/conversations/{conv.id}/messages/{msg.id}/citations", headers=headers
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert data["total"] == 1
        item = data["items"][0]
        assert item["source_type"] == "live"
        # live 必须同时携带两个 UUID。
        assert uuid.UUID(item["chunk_id"]) == chunk.id
        assert uuid.UUID(item["document_id"]) == doc.id
        # 定位/内容字段来自当前可访问来源，而非快照。
        assert item["document_version"] == 1
        assert item["filename"] == "source.txt"
        assert item["file_type"] == "txt"
        assert item["page"] == 2
        assert item["section"] == "结论"
        assert item["content"] == "来源内容预览"
        assert item["rank"] == 1
        assert item["score"] == 0.91

    def test_snapshot_citation_requires_null_ids_and_snapshot_fields(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-cite-snap@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="conv-cite-snap@example.com").one()
        conv = _conversation(db_session, user.id, kb_id)
        db_session.flush()
        msg = Message(
            user_id=user.id,
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            finish_reason=MessageFinishReason.STOP,
            content="回答",
        )
        db_session.add(msg)
        db_session.flush()
        db_session.add(
            MessageCitation(
                message_id=msg.id,
                user_id=user.id,
                knowledge_base_id=kb_id,
                chunk_id=None,
                document_id=None,
                document_version=3,
                rank=1,
                score=0.5,
                chunk_snapshot={
                    "filename": "deleted.pdf",
                    "file_type": "pdf",
                    "page": 7,
                    "section": "附录",
                    "content": "已删除内容预览",
                },
            )
        )
        db_session.commit()

        resp = client.get(
            f"/v1/conversations/{conv.id}/messages/{msg.id}/citations", headers=headers
        )
        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["source_type"] == "snapshot"
        # snapshot 必须使两个 ID 为空。
        assert item["chunk_id"] is None
        assert item["document_id"] is None
        assert item["document_version"] == 3
        assert item["filename"] == "deleted.pdf"
        assert item["file_type"] == "pdf"
        assert item["page"] == 7
        assert item["section"] == "附录"
        assert item["content"] == "已删除内容预览"

    def test_citations_ordered_by_rank_ascending_and_paginated(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "conv-cite-rank@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="conv-cite-rank@example.com").one()
        conv = _conversation(db_session, user.id, kb_id)
        db_session.flush()
        msg = Message(
            user_id=user.id,
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            finish_reason=MessageFinishReason.STOP,
            content="回答",
        )
        db_session.add(msg)
        db_session.flush()
        for rank in (3, 1, 2):
            db_session.add(
                MessageCitation(
                    message_id=msg.id,
                    user_id=user.id,
                    knowledge_base_id=kb_id,
                    chunk_id=None,
                    document_id=None,
                    document_version=1,
                    rank=rank,
                    score=float(rank) / 10,
                    chunk_snapshot={
                        "filename": f"f{rank}.txt",
                        "file_type": "txt",
                        "content": f"c{rank}",
                    },
                )
            )
        db_session.commit()

        resp = client.get(
            f"/v1/conversations/{conv.id}/messages/{msg.id}/citations?page_size=2",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert [item["rank"] for item in data["items"]] == [1, 2]
        assert data["total"] == 3
        assert data["page_size"] == 2
        assert data["page"] == 1
        resp = client.get(
            f"/v1/conversations/{conv.id}/messages/{msg.id}/citations?page=2&page_size=2",
            headers=headers,
        )
        assert [item["rank"] for item in resp.json()["data"]["items"]] == [3]

    def test_cross_user_citations_20007(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        other = User(email="conv-cite-owner@example.com", password_hash="x" * 60)
        db_session.add(other)
        db_session.flush()
        other_kb = KnowledgeBase(user_id=other.id, name="other")
        db_session.add(other_kb)
        db_session.flush()
        conv = _conversation(db_session, other.id, other_kb.id)
        db_session.flush()
        msg = Message(
            user_id=other.id,
            conversation_id=conv.id,
            role=MessageRole.USER,
            status=MessageStatus.COMPLETED,
            content="x",
        )
        db_session.add(msg)
        db_session.commit()
        tokens = _register(client, "conv-cite-intruder@example.com")
        resp = client.get(
            f"/v1/conversations/{conv.id}/messages/{msg.id}/citations", headers=_headers(tokens)
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007


class TestKnowledgeBaseCascadeDelete:
    def test_completed_kb_orchestration_cascades_conversations_messages_citations(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        """知识库完成编排清理（子资料已删、物理删除）后级联删除对话、消息与引用。"""
        tokens = _register(client, "conv-kb-del@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="conv-kb-del@example.com").one()
        conv = _conversation(db_session, user.id, kb_id)
        db_session.flush()
        msg = Message(
            user_id=user.id,
            conversation_id=conv.id,
            role=MessageRole.USER,
            status=MessageStatus.COMPLETED,
            content="hi",
        )
        db_session.add(msg)
        db_session.flush()
        db_session.add(
            MessageCitation(
                message_id=msg.id,
                user_id=user.id,
                knowledge_base_id=kb_id,
                chunk_id=None,
                document_id=None,
                document_version=1,
                rank=1,
                score=0.5,
                chunk_snapshot={"filename": "x.txt", "file_type": "txt", "content": "x"},
            )
        )
        db_session.commit()
        assert db_session.query(Conversation).filter_by(id=conv.id).count() == 1
        # 编排清理完成后物理删除知识库（T081 编排的最终动作）。
        db_session.execute(delete(Document).where(Document.knowledge_base_id == kb_id))
        db_session.delete(db_session.get(KnowledgeBase, kb_id))
        db_session.commit()
        resp = client.get(f"/v1/conversations/{conv.id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007
        assert db_session.query(Conversation).count() == 0
        assert db_session.query(Message).count() == 0
        assert db_session.query(MessageCitation).count() == 0
