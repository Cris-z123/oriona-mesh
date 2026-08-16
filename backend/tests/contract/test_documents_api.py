"""资料上传 API 契约测试（T037 / FR-004、FR-024、FR-025、FR-033）。

覆盖：PDF/DOCX/MD/TXT 四类格式接受；``20009/400`` 不支持格式、``20003/400`` 单文件
超过 50MB、``20004/400`` 单次超过 20 个文件均整批拒绝且零业务副作用；统一错误信封
（code/data/msg/trace_id）；跨用户/不存在知识库 ``20002/404``。需要真实 Redis 与
测试数据库。
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.upload_request import DocumentUploadRequest

pytestmark = pytest.mark.contract

# 50MB 上限与 20 个文件上限（quickstart / openapi.yaml）。
_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
_MAX_FILES = 20

_KB_NOT_FOUND_MSG = "请求的知识库不存在"


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


class TestAcceptSupportedFormats:
    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("a.pdf", b"%PDF-1.4\n% fake pdf"),
            ("b.docx", b"PK\x03\x04 fake docx"),
            ("c.md", b"# title\n\nbody"),
            ("d.txt", b"plain text"),
        ],
    )
    def test_upload_supported_format_202_queued(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys, name, content
    ) -> None:
        tokens = _register(client, f"up-{name.split('.')[0]}@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [(name, content)], headers)
        assert resp.status_code == 202
        body = resp.json()
        assert body["code"] == 0
        assert body["msg"] == ""
        assert uuid.UUID(body["trace_id"]) is not None
        items = body["data"]["documents"]
        assert len(items) == 1
        item = items[0]
        assert item["filename"] == name
        assert item["status"] == "queued"
        assert item["current_task_type"] == "parse"
        assert item["error_code"] is None
        assert item["error_message"] is None
        assert item["version"] == 1
        assert item["retry_count"] == 0
        assert item["delete_cycle"] == 0
        assert item["allowed_actions"] == ["delete"]
        assert item["knowledge_base_id"] == kb_id

    def test_upload_multiple_files_202_all_queued(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "up-multi@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        files = [("a.pdf", b"%pdf"), ("b.md", b"# md"), ("c.txt", b"txt"), ("d.docx", b"PK")]
        resp = _upload(client, kb_id, files, headers)
        assert resp.status_code == 202
        items = resp.json()["data"]["documents"]
        assert {item["filename"] for item in items} == {name for name, _ in files}
        assert all(item["status"] == "queued" for item in items)


class TestRejectInvalidBatch:
    def test_unsupported_file_type_20009(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "up-bad@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("evil.exe", b"MZ")], headers)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 20009
        assert body["msg"] == "仅支持 PDF、DOCX、MD 和 TXT 文件"
        assert body["data"] is None
        assert uuid.UUID(body["trace_id"]) is not None

    def test_file_too_large_20003(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "up-big@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("big.pdf", b"x" * (_MAX_FILE_SIZE_BYTES + 1))], headers)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 20003
        assert body["msg"] == "文件超过 50MB 限制"

    def test_too_many_files_20004(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "up-many@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        files = [(f"f{i}.txt", b"x") for i in range(_MAX_FILES + 1)]
        resp = _upload(client, kb_id, files, headers)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 20004
        assert body["msg"] == "单次上传最多 20 个文件"

    def test_mixed_batch_with_any_invalid_rejects_whole_batch_no_side_effects(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "up-mixed@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        files = [("ok.pdf", b"%pdf"), ("bad.xyz", b"data")]
        resp = _upload(client, kb_id, files, headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 20009
        # 整批拒绝：不创建任何资料、任务、幂等结果或文件对象。
        assert db_session.query(Document).count() == 0
        assert db_session.query(DocumentTask).count() == 0
        assert db_session.query(DocumentUploadRequest).count() == 0

    def test_invalid_batch_does_not_create_any_objects(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "up-none@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("a.txt", b"x" * (_MAX_FILE_SIZE_BYTES + 1))], headers)
        assert resp.status_code == 400
        assert db_session.query(Document).count() == 0
        assert db_session.query(DocumentTask).count() == 0
        assert db_session.query(DocumentUploadRequest).count() == 0


class TestUploadAuthorization:
    def test_upload_to_missing_knowledge_base_20002(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "up-ghost@example.com")
        resp = _upload(client, str(uuid.uuid4()), [("a.txt", b"x")], _headers(tokens))
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 20002
        assert body["msg"] == _KB_NOT_FOUND_MSG

    def test_upload_to_cross_user_knowledge_base_20002(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens_a = _register(client, "up-owner@example.com")
        kb_id = _create_kb(client, _headers(tokens_a))
        tokens_b = _register(client, "up-intruder@example.com")
        resp = _upload(client, kb_id, [("a.txt", b"x")], _headers(tokens_b))
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
