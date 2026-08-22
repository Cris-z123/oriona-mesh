"""分级限流契约测试（T030）。

覆盖：注册/登录 IP+邮箱双重阈值、刷新 IP+token HMAC 指纹、上传/问答/默认策略、
``Retry-After`` 与统一信封、超限零业务副作用、原始 token 不进 Redis/日志、
默认伪造 XFF 不生效。需要真实 Redis 与测试数据库；任一不可用时跳过。
"""

import os as _os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token, generate_refresh_token

pytestmark = pytest.mark.contract

# 测试用默认阈值（与 quickstart 一致）。
AUTH_IP_LIMIT = 20
AUTH_ACCOUNT_LIMIT = 5
UPLOAD_LIMIT = 10
QUESTION_LIMIT = 20
DEFAULT_LIMIT = 120

# 真实签名 token：中间件按解码出的用户 sub 计键（FR-026 每用户预算）。
_BEARER = {
    "Authorization": "Bearer "
    + create_access_token("contract-rate-limit-user", _os.environ["AUTH_JWT_SECRET_KEY"])
}
_KB_UUID = "00000000-0000-4000-8000-000000000001"
_CID_UUID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _ensure_schema(test_engine):
    """保证 schema 存在：本文件只用 client（不触发 db_session），单独运行
    （如 T086 门禁）时若未建表会得到 500/ProgrammingError。"""
    yield


@pytest.fixture(autouse=True)
def _clean_rate_limit_keys(redis_client) -> Generator[None, None, None]:
    """每测试清空限流键，避免 IP/账号预算跨测试泄漏。"""
    yield
    for key in redis_client.scan_iter("rl:*"):
        redis_client.delete(key)


def _register(client: TestClient, email: str):
    # FR-001（阶段 12）：注册密码必须同时含字母和数字，否则服务端 400 无法消耗预算。
    return client.post("/v1/users", json={"email": email, "password": "password123"})


class TestAuthIpAndAccount:
    def test_account_budget_shared_across_register_and_login(self, client: TestClient) -> None:
        email = "shared-account@example.com"
        statuses = []
        for _ in range(AUTH_ACCOUNT_LIMIT):
            statuses.append(_register(client, email).status_code)
        assert 201 in statuses or 409 in statuses  # 首次成功或已存在
        denied = _register(client, email)
        assert denied.status_code == 429
        body = denied.json()
        assert body["code"] == 10005
        assert body["msg"] == "请求过于频繁，请稍后再试"
        assert denied.headers.get("Retry-After") is not None
        assert "trace_id" in body

    def test_ip_budget_independent_of_account(self, client: TestClient) -> None:
        # 不同账号、同一来源 IP：第 AUTH_IP_LIMIT+1 次被 IP 规则拒绝。
        responses = [_register(client, f"ip-{i}@example.com") for i in range(AUTH_IP_LIMIT + 1)]
        assert responses[-1].status_code == 429
        assert responses[-1].json()["code"] == 10005

    def test_default_forged_xff_ignored(self, client: TestClient) -> None:
        # 无可信代理配置：伪造 X-Forwarded-For 不改变来源 IP（与直连对端共用预算）。
        responses = [
            client.post(
                "/v1/users",
                json={"email": f"xff-{i}@example.com", "password": "x" * 8},
                headers={"X-Forwarded-For": "203.0.113.99"},
            )
            for i in range(AUTH_IP_LIMIT + 1)
        ]
        assert responses[-1].status_code == 429
        assert responses[-1].json()["code"] == 10005


class TestRefreshIpAndToken:
    def test_token_fingerprint_budget(self, client: TestClient) -> None:
        token = generate_refresh_token()
        responses = [
            client.put("/v1/auth/sessions", json={"refresh_token": token})
            for _ in range(AUTH_ACCOUNT_LIMIT + 1)
        ]
        assert responses[-1].status_code == 429
        assert responses[-1].json()["code"] == 10005
        assert responses[-1].headers.get("Retry-After") is not None

    def test_distinct_tokens_have_independent_budgets(self, client: TestClient) -> None:
        for _ in range(AUTH_ACCOUNT_LIMIT):
            client.put("/v1/auth/sessions", json={"refresh_token": generate_refresh_token()})
        # 每个 token 只用了 1 次，未超限。
        resp = client.put("/v1/auth/sessions", json={"refresh_token": generate_refresh_token()})
        assert resp.status_code != 429

    def test_raw_token_not_in_redis(self, client: TestClient, redis_client) -> None:
        token = generate_refresh_token()
        for _ in range(3):
            client.put("/v1/auth/sessions", json={"refresh_token": token})
        for key in redis_client.scan_iter("rl:*"):
            assert token not in key
            assert "example.com" not in key


class TestUserPolicies:
    def test_upload_user_policy(self, client: TestClient) -> None:
        responses = [
            client.post(f"/v1/knowledge-bases/{_KB_UUID}/documents", headers=_BEARER)
            for _ in range(UPLOAD_LIMIT + 1)
        ]
        assert responses[-1].status_code == 429
        assert responses[-1].json()["code"] == 10005

    def test_question_user_policy(self, client: TestClient) -> None:
        responses = [
            client.post(f"/v1/conversations/{_CID_UUID}/messages", headers=_BEARER)
            for _ in range(QUESTION_LIMIT + 1)
        ]
        assert responses[-1].status_code == 429
        assert responses[-1].json()["code"] == 10005

    def test_default_authenticated_policy(self, client: TestClient) -> None:
        responses = [client.get("/v1/users/me", headers=_BEARER) for _ in range(DEFAULT_LIMIT + 1)]
        assert responses[-1].status_code == 429
        assert responses[-1].json()["code"] == 10005


class TestNoBusinessSideEffects:
    def test_denied_register_creates_no_user(self, client: TestClient, db_session) -> None:
        from sqlalchemy import func, select

        from app.models.user import User

        email = "side-effect@example.com"
        for _ in range(AUTH_ACCOUNT_LIMIT):
            _register(client, email)
        before = db_session.scalar(select(func.count()).select_from(User))
        denied = _register(client, email)
        assert denied.status_code == 429
        after = db_session.scalar(select(func.count()).select_from(User))
        assert after == before

    def test_denied_refresh_creates_no_session(self, client: TestClient, db_session) -> None:
        from sqlalchemy import func, select

        from app.models.auth_session import AuthSession

        token = generate_refresh_token()
        for _ in range(AUTH_ACCOUNT_LIMIT):
            client.put("/v1/auth/sessions", json={"refresh_token": token})
        before = db_session.scalar(select(func.count()).select_from(AuthSession))
        denied = client.put("/v1/auth/sessions", json={"refresh_token": token})
        assert denied.status_code == 429
        after = db_session.scalar(select(func.count()).select_from(AuthSession))
        assert after == before
