"""业务错误码契约测试（T086 / openapi.yaml ErrorCode 与各错误信封 schema）。

契约来源（openapi.yaml ErrorCode 枚举与 TokenExpired / InvalidCredentials /
InvalidRefreshToken / UploadValidationError / RateLimitExceeded /
ProtectionUnavailable / InternalError 错误信封）：
- Access Token 全部验证失败统一 ``10001/401`` "请重新登录"：缺失 Bearer 头、Bearer
  后无 token、非三段格式、其他密钥签名、算法无效（alg=none / HS512）、必填声明缺失、
  type=refresh、过期；
- ``10004/401`` 仅用于登录凭证错误：错误密码与未注册邮箱同一提示，不泄露账号存在性；
  token 校验失败绝不映射 10004；
- ``10006/401`` 无效/过期 refresh token（模式合规但未知/过期的 token；模式不符的
  token 在参数校验层 ``10003/400`` 拒绝，见 openapi RefreshSessionInput pattern）；
- ``20009/400`` 不支持格式整批拒绝且零业务副作用；
- 异步失败码 ``20001/20010~20015/50000`` 映射固定安全提示（Document.error_message）；
- ``10005/429`` 超限响应含 ``Retry-After`` 头、统一信封与 trace_id；
- ``50001/503`` 限流保护不可用（Redis 故障）fail-closed，状态变更无业务副作用；
  只读 GET 按 ``RATE_LIMIT_READ_FAIL_OPEN`` 当前配置（默认 true）降级放行；
- 所有非 SSE 端点未分类服务端异常统一 ``50000/500`` 信封、trace_id 为 UUID、
  data 为 None 且不泄漏异常详情（SSE 消息发送端点的 50000 走 SSE error 事件，
  不属本文件范围）。需要真实 Redis 与测试数据库。
"""

import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
import redis as redis_lib
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.schemas.common import DEFAULT_ERROR_MSG
from app.api.v1.schemas.documents import (
    ASYNC_ERROR_MESSAGES,
    document_dto,
    document_task_dto,
)
from app.core.security import create_access_token, generate_refresh_token, refresh_token_hash
from app.core.settings import get_settings
from app.infrastructure.rate_limit.redis_limiter import (
    RateLimitDecision,
    RedisSlidingWindowLimiter,
)
from app.models.auth_session import AuthSession
from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.enums import (
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    FileType,
)
from app.models.user import User

pytestmark = pytest.mark.contract

_JWT_SECRET = os.environ["AUTH_JWT_SECRET_KEY"]
# 注册/登录账号维度限流阈值（quickstart / test_rate_limits.py 同源）。
_AUTH_ACCOUNT_LIMIT = 5
# 未知资料/会话资源 ID：对应服务方法已打桩，请求无需命中真实资源。
_DOC_UUID = "00000000-0000-4000-8000-00000000000a"
_CONV_UUID = "00000000-0000-4000-8000-00000000000b"
_RANDOM_REFRESH_TOKEN = "rt_" + "A" * 43

# 异步失败码 → openapi Document.error_message 固定安全提示。
_ASYNC_EXPECTED_MESSAGES: dict[int, str] = {
    20001: "资料解析失败，请删除后重新上传",
    20010: "资料内容为空，请删除后重新上传",
    20011: "文件保存失败，请删除后重新上传",
    20012: "资料向量化失败，请删除后重新上传",
    20013: "资料处理结果不一致，请删除后重新上传",
    20014: "资料处理失败，请删除后重新上传",
    20015: "资料删除未完成，请重试删除",
    50000: DEFAULT_ERROR_MSG,
}


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema(test_engine):
    """本模块部分测试不注入 db_session；依赖本夹具以触发会话级 test_engine schema 创建。"""
    yield


def _assert_valid_trace_id(value: str) -> None:
    """契约要求 trace_id 为合法 UUID。"""
    assert isinstance(value, str)
    uuid.UUID(value)


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient, email: str, password: str = "password123"):
    return client.post("/v1/users", json={"email": email, "password": password})


def _register_tokens(client: TestClient, email: str) -> dict:
    """API 注册 + 登录，返回会话令牌（复用 test_documents_api 的 helper 模式）。"""
    assert _register(client, email).status_code == 201
    resp = client.post("/v1/auth/sessions", json={"email": email, "password": "password123"})
    assert resp.status_code == 201
    return resp.json()["data"]


def _create_kb(client: TestClient, headers: dict, name: str = "kb") -> str:
    resp = client.post("/v1/knowledge-bases", json={"name": name}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


def _upload(client: TestClient, kb_id: str, files: list[tuple[str, bytes]], headers: dict):
    multipart = [("files", (name, content, "application/octet-stream")) for name, content in files]
    return client.post(f"/v1/knowledge-bases/{kb_id}/documents", files=multipart, headers=headers)


def _signed(payload: dict, *, key: str | None = _JWT_SECRET, algorithm: str = "HS256") -> str:
    """手工签发测试 token；alg=none 时不传密钥（PyJWT 要求 key=None）。"""
    return jwt.encode(payload, key, algorithm=algorithm)


def _assert_internal_error(resp: Response) -> None:
    """未分类服务端异常的统一 50000/500 信封断言（openapi InternalErrorErrorEnvelope）。"""
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == 50000
    assert body["msg"] == "系统繁忙，请稍后再试"
    assert body["data"] is None
    _assert_valid_trace_id(body["trace_id"])
    assert resp.headers["X-Trace-Id"] == body["trace_id"]
    # 不得泄漏异常详情或内部信息。
    assert "boom-secret-t086" not in resp.text
    assert "RuntimeError" not in resp.text


# ---------------------------------------------------------------------------
# 1. Access Token 全部验证失败统一 10001/401（openapi TokenExpired）
# ---------------------------------------------------------------------------
_SUB = "00000000-0000-4000-8000-000000000000"
_now = datetime.now(UTC)
_VALID_CLAIMS = {
    "sub": _SUB,
    "iat": _now,
    "exp": _now + timedelta(hours=1),
    "type": "access",
}

_ACCESS_TOKEN_FAILURE_CASES = [
    pytest.param(None, id="missing-bearer-header"),
    pytest.param("Bearer", id="bearer-without-token"),
    pytest.param("Bearer ", id="bearer-trailing-whitespace"),
    pytest.param("Bearer not-a-jwt", id="malformed-not-three-segments"),
    pytest.param(
        "Bearer " + _signed({**_VALID_CLAIMS}, key="another-secret-" + "z" * 32),
        id="invalid-signature-other-key",
    ),
    pytest.param("Bearer " + _signed(_VALID_CLAIMS, algorithm="none", key=None), id="alg-none"),
    pytest.param("Bearer " + _signed(_VALID_CLAIMS, algorithm="HS512"), id="alg-hs512"),
    pytest.param("Bearer " + _signed({"sub": _SUB}), id="missing-required-claims"),
    pytest.param(
        "Bearer " + _signed({**_VALID_CLAIMS, "type": "refresh"}), id="wrong-type-refresh"
    ),
    pytest.param(
        "Bearer "
        + _signed(
            {
                "sub": _SUB,
                "iat": _now - timedelta(hours=3),
                "exp": _now - timedelta(hours=1),
                "type": "access",
            }
        ),
        id="expired-token",
    ),
]


class TestAccessTokenFailuresUniform10001:
    @pytest.mark.parametrize("header", _ACCESS_TOKEN_FAILURE_CASES)
    def test_all_access_token_failures_return_10001(
        self, client: TestClient, clean_rate_limit_keys, header: str | None
    ) -> None:
        headers = {} if header is None else {"Authorization": header}
        resp = client.get("/v1/users/me", headers=headers)
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 10001
        assert body["msg"] == "请重新登录"
        assert body["data"] is None
        _assert_valid_trace_id(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]


# ---------------------------------------------------------------------------
# 2. 10004/401 仅登录凭证错误（openapi InvalidCredentials / Unauthorized）
# ---------------------------------------------------------------------------
class TestLoginCredentialsOnly10004:
    def test_wrong_password_10004(self, client: TestClient, clean_rate_limit_keys) -> None:
        _register(client, "t086-cred@example.com")
        resp = client.post(
            "/v1/auth/sessions",
            json={"email": "t086-cred@example.com", "password": "wrong-password"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 10004
        assert body["msg"] == "邮箱或密码错误"
        assert body["data"] is None
        _assert_valid_trace_id(body["trace_id"])

    def test_unknown_email_10004_no_account_enumeration(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        resp = client.post(
            "/v1/auth/sessions",
            json={"email": "t086-ghost@example.com", "password": "password123"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 10004
        assert body["msg"] == "邮箱或密码错误"  # 与错误密码同提示，不泄露账号存在性

    def test_token_validation_failures_never_map_10004(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        # 与 10001 组呼应：token 校验失败（即使伪造签名）统一 10001，绝不映射 10004。
        resp = client.get("/v1/users/me", headers=_auth_header("not-a-jwt"))
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 10001
        assert body["code"] != 10004


# ---------------------------------------------------------------------------
# 2a. 注册密码规则与知识库规范化名称冲突（US4 / FR-001、FR-003）
# ---------------------------------------------------------------------------
class TestCoreWorkflowValidation:
    @pytest.mark.parametrize(
        ("email", "password"),
        [
            ("t142-short@example.com", "abc1234"),
            ("t142-no-letter@example.com", "12345678"),
            ("t142-no-digit@example.com", "abcdefgh"),
        ],
    )
    def test_registration_rejects_password_without_required_strength(
        self,
        client: TestClient,
        db_session: Session,
        clean_rate_limit_keys,
        email: str,
        password: str,
    ) -> None:
        """FR-001：服务端拒绝短密码、纯数字和纯字母密码，且不创建用户。"""
        before = db_session.scalar(select(func.count()).select_from(User))
        resp = _register(client, email, password=password)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 10003
        assert body["data"] is None
        assert db_session.scalar(select(func.count()).select_from(User)) == before

    def test_create_and_rename_normalized_name_conflicts_return_20016(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        """FR-003：同一用户的 trim + casefold 名称冲突统一映射为 20016/409。"""
        tokens = _register_tokens(client, "t142-names@example.com")
        headers = _auth_header(tokens["access_token"])
        first = _create_kb(client, headers, "  Research Notes  ")

        created = client.post(
            "/v1/knowledge-bases", json={"name": "research notes"}, headers=headers
        )
        assert created.status_code == 409
        assert created.json()["code"] == 20016
        assert created.json()["msg"] == "知识库名称已存在，请更换名称"

        second = _create_kb(client, headers, "Personal")
        renamed = client.patch(
            f"/v1/knowledge-bases/{second}",
            json={"name": "  RESEARCH NOTES  "},
            headers=headers,
        )
        assert renamed.status_code == 409
        assert renamed.json()["code"] == 20016
        assert renamed.json()["msg"] == "知识库名称已存在，请更换名称"
        assert first != second


# ---------------------------------------------------------------------------
# 3. 10006/401 无效/过期 refresh token（openapi InvalidRefreshToken）
# ---------------------------------------------------------------------------
class TestInvalidRefreshToken10006:
    def test_unknown_random_token_10006(self, client: TestClient, clean_rate_limit_keys) -> None:
        resp = client.put("/v1/auth/sessions", json={"refresh_token": _RANDOM_REFRESH_TOKEN})
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 10006
        assert body["msg"] == "登录状态已失效，请重新登录"
        assert body["data"] is None
        _assert_valid_trace_id(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]

    def test_expired_session_token_10006(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        user = User(email="t086-expired@example.com", password_hash="x" * 60)
        db_session.add(user)
        db_session.flush()
        raw = generate_refresh_token()
        db_session.add(
            AuthSession(
                user_id=user.id,
                refresh_token_hash=refresh_token_hash(raw),
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        db_session.commit()
        resp = client.put("/v1/auth/sessions", json={"refresh_token": raw})
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 10006
        assert body["msg"] == "登录状态已失效，请重新登录"

    def test_schema_violating_token_rejected_at_validation_layer(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        # openapi RefreshSessionInput 模式 rt_+43 位 Base64URL：模式不符在参数层
        # 10003/400 拒绝，不进入 10006 语义；10006 仅覆盖模式合规但无效/过期/已撤销/
        # 重放的 token。
        resp = client.put("/v1/auth/sessions", json={"refresh_token": "garbage"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 10003
        assert body["msg"] == "请求参数不合法，请检查后重试"


# ---------------------------------------------------------------------------
# 4. 20009/400 不支持格式整批拒绝（openapi UploadValidationError）
# ---------------------------------------------------------------------------
class TestUnsupportedFileType20009:
    def test_upload_unsupported_format_rejects_whole_batch(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register_tokens(client, "t086-up@example.com")
        kb_id = _create_kb(client, _auth_header(tokens["access_token"]))
        resp = _upload(client, kb_id, [("evil.exe", b"MZ")], _auth_header(tokens["access_token"]))
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 20009
        assert body["msg"] == "仅支持 PDF、DOCX、MD 和 TXT 文件"
        assert body["data"] is None
        _assert_valid_trace_id(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]


# ---------------------------------------------------------------------------
# 5. 异步失败码 20001/20010~20015/50000 固定安全提示（openapi Document.error_message）
# ---------------------------------------------------------------------------
class TestAsyncFailureMessages:
    def test_mapping_table_matches_openapi(self) -> None:
        assert ASYNC_ERROR_MESSAGES == _ASYNC_EXPECTED_MESSAGES

    @pytest.mark.parametrize("code", sorted(_ASYNC_EXPECTED_MESSAGES))
    def test_document_dto_maps_each_async_code(self, code: int) -> None:
        doc = Document(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            filename="a.pdf",
            file_type=FileType.PDF,
            file_size=10,
            storage_path="a.pdf",
            upload_batch_id=uuid.uuid4(),
            content_hash="c" * 64,
            status=DocumentStatus.FAILED,
            error_code=code,
        )
        dto = document_dto(doc)
        assert dto["error_code"] == code
        assert dto["error_message"] == _ASYNC_EXPECTED_MESSAGES[code]

    def test_document_task_dto_maps_async_code(self) -> None:
        task = DocumentTask(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            knowledge_base_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_version=1,
            task_type=DocumentTaskType.PARSE,
            status=DocumentTaskStatus.FAILED,
            idempotency_key="t086-async-map",
            error_code=20001,
        )
        dto = document_task_dto(task, attempts=[])
        assert dto["error_code"] == 20001
        assert dto["error_message"] == _ASYNC_EXPECTED_MESSAGES[20001]


# ---------------------------------------------------------------------------
# 6. 10005/429 超限统一信封 + Retry-After（openapi RateLimitExceeded）
# ---------------------------------------------------------------------------
class TestRateLimitExceeded10005:
    def test_over_account_budget_returns_10005_envelope(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        email = "t086-rate@example.com"
        payload = {"email": email, "password": "password123"}
        for _ in range(_AUTH_ACCOUNT_LIMIT):
            client.post("/v1/users", json=payload)
        resp = client.post("/v1/users", json=payload)
        assert resp.status_code == 429
        body = resp.json()
        assert body["code"] == 10005
        assert body["msg"] == "请求过于频繁，请稍后再试"
        assert body["data"] is None
        assert int(resp.headers["Retry-After"]) >= 1
        _assert_valid_trace_id(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]


# ---------------------------------------------------------------------------
# 7. 50001/503 限流保护不可用 fail-closed（openapi ProtectionUnavailable）
# ---------------------------------------------------------------------------
class TestProtectionUnavailable50001:
    @pytest.fixture
    def redis_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 Redis 不可用：限流器 check 抛 RedisError，由中间件按端点类别降级。"""

        def _check(
            self: RedisSlidingWindowLimiter, key: str, limit: int, window_seconds: int
        ) -> RateLimitDecision:
            raise redis_lib.ConnectionError("simulated redis outage (t086)")

        monkeypatch.setattr(RedisSlidingWindowLimiter, "check", _check)

    def _assert_50001(self, resp: Response) -> None:
        assert resp.status_code == 503
        body = resp.json()
        assert body["code"] == 50001
        assert body["msg"] == "系统繁忙，请稍后再试"
        assert body["data"] is None
        _assert_valid_trace_id(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]

    def test_register_fail_closed_50001_no_side_effect(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys, redis_down
    ) -> None:
        # 中间件在业务写入前拦截：注册未产生用户。
        before = db_session.scalar(select(func.count()).select_from(User))
        resp = client.post(
            "/v1/users", json={"email": "t086-failclosed@example.com", "password": "password123"}
        )
        self._assert_50001(resp)
        after = db_session.scalar(select(func.count()).select_from(User))
        assert after == before

    def test_login_fail_closed_50001(
        self, client: TestClient, clean_rate_limit_keys, redis_down
    ) -> None:
        resp = client.post(
            "/v1/auth/sessions",
            json={"email": "t086-login@example.com", "password": "password123"},
        )
        self._assert_50001(resp)

    def test_read_fail_open_by_current_config(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys, redis_down
    ) -> None:
        # 断言当前配置语义：RATE_LIMIT_READ_FAIL_OPEN 默认 true，只读 GET 降级放行。
        assert get_settings().rate_limit.read_fail_open is True
        user = User(email="t086-read@example.com", password_hash="x" * 60)
        db_session.add(user)
        db_session.commit()
        headers = _auth_header(create_access_token(str(user.id), _JWT_SECRET))
        resp = client.get("/v1/users/me", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["code"] == 0


# ---------------------------------------------------------------------------
# 8. 所有非 SSE 端点未分类异常统一 50000/500（openapi InternalError）
# ---------------------------------------------------------------------------
def _req_register(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.post(
        "/v1/users", json={"email": "t086-reg@example.com", "password": "password123"}
    )


def _req_login(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.post(
        "/v1/auth/sessions", json={"email": "t086-login@example.com", "password": "password123"}
    )


def _req_refresh(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.put("/v1/auth/sessions", json={"refresh_token": _RANDOM_REFRESH_TOKEN})


def _req_logout(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.request(
        "DELETE",
        "/v1/auth/sessions",
        json={"refresh_token": _RANDOM_REFRESH_TOKEN},
        headers=headers,
    )


def _req_get_me(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get("/v1/users/me", headers=headers)


def _req_patch_me(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.patch("/v1/users/me", json={"display_name": "t086"}, headers=headers)


def _req_kb_list(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get("/v1/knowledge-bases", headers=headers)


def _req_kb_create(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.post("/v1/knowledge-bases", json={"name": "t086-kb"}, headers=headers)


def _req_kb_get(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get(f"/v1/knowledge-bases/{kb_id}", headers=headers)


def _req_kb_patch(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.patch(f"/v1/knowledge-bases/{kb_id}", json={"name": "t086-kb2"}, headers=headers)


def _req_kb_delete(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)


def _req_upload(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    files = [("files", ("a.pdf", b"%PDF-1.4", "application/octet-stream"))]
    return client.post(f"/v1/knowledge-bases/{kb_id}/documents", files=files, headers=headers)


def _req_doc_list(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)


def _req_doc_get(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get(f"/v1/knowledge-bases/{kb_id}/documents/{_DOC_UUID}", headers=headers)


def _req_doc_delete(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{_DOC_UUID}", headers=headers)


def _req_doc_tasks(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get(f"/v1/knowledge-bases/{kb_id}/documents/{_DOC_UUID}/tasks", headers=headers)


def _req_conv_list(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get("/v1/conversations", headers=headers)


def _req_conv_create(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.post(
        "/v1/conversations",
        json={"knowledge_base_id": kb_id, "title": "t086"},
        headers=headers,
    )


def _req_conv_get(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get(f"/v1/conversations/{_CONV_UUID}", headers=headers)


def _req_conv_rename(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.patch(
        f"/v1/conversations/{_CONV_UUID}", json={"title": "t086-2"}, headers=headers
    )


def _req_conv_delete(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.delete(f"/v1/conversations/{_CONV_UUID}", headers=headers)


def _req_msg_list(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get(f"/v1/conversations/{_CONV_UUID}/messages", headers=headers)


def _req_citations(client: TestClient, headers: dict, kb_id: str | None) -> Response:
    return client.get(
        f"/v1/conversations/{_CONV_UUID}/messages/{_DOC_UUID}/citations", headers=headers
    )


# (monkeypatch 目标, 属性名, 请求构造器, 是否需要真实知识库)
_INTERNAL_ERROR_CASES = [
    pytest.param(
        "app.services.auth_service.AuthService",
        "register",
        _req_register,
        False,
        id="auth-register",
    ),
    pytest.param(
        "app.services.auth_service.AuthService", "login", _req_login, False, id="auth-login"
    ),
    pytest.param(
        "app.services.auth_service.AuthService", "refresh", _req_refresh, False, id="auth-refresh"
    ),
    pytest.param(
        "app.services.auth_service.AuthService", "logout", _req_logout, False, id="auth-logout"
    ),
    pytest.param("app.api.v1.routes.users", "user_dto", _req_get_me, False, id="users-me-get"),
    pytest.param(
        "app.services.user_service.UserService",
        "update_profile",
        _req_patch_me,
        False,
        id="users-me-patch",
    ),
    pytest.param(
        "app.services.knowledge_base_service.KnowledgeBaseService",
        "list_for_user",
        _req_kb_list,
        False,
        id="knowledge-bases-list",
    ),
    pytest.param(
        "app.services.knowledge_base_service.KnowledgeBaseService",
        "create",
        _req_kb_create,
        False,
        id="knowledge-bases-create",
    ),
    pytest.param(
        "app.services.knowledge_base_service.KnowledgeBaseService",
        "get",
        _req_kb_get,
        True,
        id="knowledge-bases-detail",
    ),
    pytest.param(
        "app.services.knowledge_base_service.KnowledgeBaseService",
        "update",
        _req_kb_patch,
        True,
        id="knowledge-bases-update",
    ),
    pytest.param(
        "app.services.knowledge_base_service.KnowledgeBaseService",
        "delete",
        _req_kb_delete,
        True,
        id="knowledge-bases-delete",
    ),
    pytest.param(
        "app.services.document_service.DocumentService",
        "upload",
        _req_upload,
        True,
        id="documents-upload",
    ),
    pytest.param(
        "app.services.document_status_service.DocumentStatusService",
        "list_documents",
        _req_doc_list,
        True,
        id="documents-list",
    ),
    pytest.param(
        "app.services.document_status_service.DocumentStatusService",
        "get_document",
        _req_doc_get,
        True,
        id="documents-detail",
    ),
    pytest.param(
        "app.services.document_deletion_service.DocumentDeletionService",
        "delete",
        _req_doc_delete,
        True,
        id="documents-delete",
    ),
    pytest.param(
        "app.services.document_status_service.DocumentStatusService",
        "list_tasks",
        _req_doc_tasks,
        True,
        id="documents-tasks",
    ),
    pytest.param(
        "app.services.conversation_service.ConversationService",
        "list_conversations",
        _req_conv_list,
        False,
        id="conversations-list",
    ),
    pytest.param(
        "app.services.conversation_service.ConversationService",
        "create",
        _req_conv_create,
        True,
        id="conversations-create",
    ),
    pytest.param(
        "app.services.conversation_service.ConversationService",
        "get",
        _req_conv_get,
        False,
        id="conversations-detail",
    ),
    pytest.param(
        "app.services.conversation_service.ConversationService",
        "rename",
        _req_conv_rename,
        False,
        id="conversations-rename",
    ),
    pytest.param(
        "app.services.conversation_service.ConversationService",
        "delete",
        _req_conv_delete,
        False,
        id="conversations-delete",
    ),
    pytest.param(
        "app.services.conversation_service.ConversationService",
        "list_messages",
        _req_msg_list,
        False,
        id="messages-list",
    ),
    pytest.param(
        "app.services.citation_service.CitationService",
        "list_for_message",
        _req_citations,
        False,
        id="citations-list",
    ),
]


class TestUnclassifiedInternalError50000:
    @pytest.fixture
    def owner_headers(self, db_session: Session) -> dict:
        """直接建库内用户并签发真实 Access Token（跳过 API 注册，避免限流计数干扰）。"""
        user = User(email=f"t086-{uuid.uuid4().hex[:12]}@example.com", password_hash="x" * 60)
        db_session.add(user)
        db_session.commit()
        return _auth_header(create_access_token(str(user.id), _JWT_SECRET))

    @pytest.mark.parametrize(
        ("target", "attr", "request_builder", "needs_kb"), _INTERNAL_ERROR_CASES
    )
    def test_unclassified_error_wraps_50000(
        self,
        client: TestClient,
        owner_headers: dict,
        clean_rate_limit_keys,
        monkeypatch: pytest.MonkeyPatch,
        target: str,
        attr: str,
        request_builder: Callable[[TestClient, dict, str | None], Response],
        needs_kb: bool,
    ) -> None:
        """每个非 SSE 端点服务层抛未分类异常 → 50000/500 统一信封，不泄漏异常细节。"""

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom-secret-t086")

        # 二参字符串形式：按点分路径解析为「模块/类.属性」后打桩。
        monkeypatch.setattr(f"{target}.{attr}", boom)
        kb_id = _create_kb(client, owner_headers) if needs_kb else None
        resp = request_builder(client, owner_headers, kb_id)
        _assert_internal_error(resp)


class TestRoutingErrors10003:
    """路由层 HTTPException（T084 冻结语义）：非 404 映射 10003 并保留 HTTP 状态码。

    openapi.yaml 契约：除 SSE 外所有响应均为统一信封；405 方法不允许为客户端
    请求错误，业务码 10003（"请求参数不合法，请检查后重试"），不得用 50000
    （内部错误）表达。415 由 FastAPI 对错误 Content-Type 的 JSON 解析失败统一
    收敛为 400/10003，不产生 415 状态码（见单元测试映射一致性）。
    """

    def test_method_not_allowed_405_uniform_envelope(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        # 已有路径（/v1/users 仅定义 POST）用错误方法 → Starlette 405，
        # 保留 405 状态码并以 10003 业务码返回统一信封。
        resp = client.put(
            "/v1/users", json={"email": "routing-405@example.com", "password": "x" * 8}
        )
        assert resp.status_code == 405
        body = resp.json()
        assert body["code"] == 10003
        assert body["msg"] == "请求参数不合法，请检查后重试"
        assert body["data"] is None
        uuid.UUID(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]

    def test_wrong_content_type_maps_10003_400_not_415(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        # FastAPI 对非 JSON Content-Type 的 body 解析失败映射 RequestValidationError
        # （10003/400），不会产生 Starlette 415；实现与 openapi 契约的 10003 语义一致。
        resp = client.post(
            "/v1/users",
            content="not-json",
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 10003
        assert body["msg"] == "请求参数不合法，请检查后重试"
        assert uuid.UUID(body["trace_id"]) is not None
