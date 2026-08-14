"""认证 API 契约测试（T025 / FR-001、FR-002）。

覆盖：注册后 ``last_login_at`` 为空、登录后更新、HS256 Access JWT 必填声明与固定
2 小时 TTL、随机不透明 Refresh Token、登出幂等撤销与跨用户拒绝、Access Token 全部
验证失败统一 ``10001/401``、``10004`` 仅登录凭证错误。需要真实 Redis 与测试数据库。
"""

import os
import re
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import refresh_token_hash
from app.models.auth_session import AuthSession
from app.models.user import User

pytestmark = pytest.mark.contract

_JWT_SECRET = os.environ["AUTH_JWT_SECRET_KEY"]


def _register(client: TestClient, email: str, password: str = "password123", **extra):
    return client.post("/v1/users", json={"email": email, "password": password, **extra})


def _login(client: TestClient, email: str, password: str = "password123"):
    return client.post("/v1/auth/sessions", json={"email": email, "password": password})


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _delete_with_body(client: TestClient, url: str, payload: dict, headers: dict):
    # httpx delete 不支持 json 参数；request 方法支持请求体。
    return client.request("DELETE", url, json=payload, headers=headers)


class TestRegister:
    def test_register_returns_normalized_user(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        resp = _register(client, "  User@Example.COM  ", display_name="小王")
        assert resp.status_code == 201
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["email"] == "user@example.com"  # strip + casefold
        assert data["display_name"] == "小王"
        assert re.fullmatch(r"[0-9a-f-]{36}", data["id"]) is not None

    def test_register_keeps_last_login_null(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        resp = _register(client, "fresh@example.com")
        assert resp.status_code == 201
        user = db_session.scalar(select(User).where(User.email == "fresh@example.com"))
        assert user is not None
        assert user.last_login_at is None

    def test_duplicate_email_conflict(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        assert _register(client, "dup@example.com").status_code == 201
        resp = _register(client, "DUP@example.com")
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == 20006
        assert body["msg"] == "该邮箱已注册，请直接登录"

    def test_invalid_email_400(self, client: TestClient, clean_rate_limit_keys) -> None:
        resp = _register(client, "not-an-email")
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003

    def test_short_password_400(self, client: TestClient, clean_rate_limit_keys) -> None:
        resp = _register(client, "pw@example.com", password="short")
        assert resp.status_code == 400
        assert resp.json()["code"] == 10003


class TestLogin:
    def test_login_returns_valid_tokens(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "login@example.com")
        resp = _login(client, "Login@Example.com")
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["token_type"] == "Bearer"
        assert data["expires_in"] == 7200
        # Access Token：HS256、必填声明、2 小时 TTL。
        claims = jwt.decode(data["access_token"], _JWT_SECRET, algorithms=["HS256"])
        assert set(("sub", "iat", "exp", "type")) <= set(claims)
        assert claims["type"] == "access"
        assert claims["exp"] - claims["iat"] == 7200
        header = jwt.get_unverified_header(data["access_token"])
        assert header["alg"] == "HS256"
        # Refresh Token：rt_ 前缀、46 位、非 JWT。
        token = data["refresh_token"]
        assert re.fullmatch(r"rt_[A-Za-z0-9_-]{43}", token) is not None
        assert token.count(".") == 0

    def test_login_updates_last_login_at(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "stamp@example.com")
        assert _login(client, "stamp@example.com").status_code == 201
        user = db_session.scalar(select(User).where(User.email == "stamp@example.com"))
        assert user is not None
        assert user.last_login_at is not None
        assert user.last_login_at.tzinfo is not None

    def test_wrong_password_10004(self, client: TestClient, clean_rate_limit_keys) -> None:
        _register(client, "cred@example.com")
        resp = _login(client, "cred@example.com", password="wrong-password")
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 10004
        assert body["msg"] == "邮箱或密码错误"

    def test_unknown_email_10004(self, client: TestClient, clean_rate_limit_keys) -> None:
        resp = _login(client, "ghost@example.com")
        assert resp.status_code == 401
        assert resp.json()["code"] == 10004


class TestRefresh:
    def test_refresh_rotates_session(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "rotate@example.com")
        old = _login(client, "rotate@example.com").json()["data"]["refresh_token"]
        resp = client.put("/v1/auth/sessions", json={"refresh_token": old})
        assert resp.status_code == 200
        new = resp.json()["data"]["refresh_token"]
        assert new != old
        # 旧会话已撤销；新会话记录轮换来源。
        user = db_session.scalar(select(User).where(User.email == "rotate@example.com"))
        assert user is not None
        sessions = db_session.scalars(
            select(AuthSession).where(AuthSession.user_id == user.id)
        ).all()
        assert len(sessions) == 2
        revoked = next(s for s in sessions if s.refresh_token_hash == refresh_token_hash(old))
        assert revoked.revoked_at is not None
        successor = next(s for s in sessions if s.rotated_from_session_id == revoked.id)
        assert successor is not None

    def test_replay_of_rotated_token_rejected_without_cascading(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "replay@example.com")
        old = _login(client, "replay@example.com").json()["data"]["refresh_token"]
        assert client.put("/v1/auth/sessions", json={"refresh_token": old}).status_code == 200
        # 重放旧 token：10006/401。
        resp = client.put("/v1/auth/sessions", json={"refresh_token": old})
        assert resp.status_code == 401
        assert resp.json()["code"] == 10006
        assert resp.json()["msg"] == "登录状态已失效，请重新登录"
        # 不连带撤销其他 active sessions。
        user = db_session.scalar(select(User).where(User.email == "replay@example.com"))
        assert user is not None
        open_count = db_session.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        )
        assert open_count == 1

    def test_invalid_token_10006(self, client: TestClient, clean_rate_limit_keys) -> None:
        resp = client.put("/v1/auth/sessions", json={"refresh_token": "rt_" + "A" * 43})
        assert resp.status_code == 401
        assert resp.json()["code"] == 10006

    def test_refresh_policy_uses_ip_and_token_fingerprint(
        self, client: TestClient, redis_client, clean_rate_limit_keys
    ) -> None:
        # 断言限流键使用 token HMAC 指纹：键中不得出现 token 原值。
        from app.core.security import generate_refresh_token

        token = generate_refresh_token()
        for _ in range(3):
            client.put("/v1/auth/sessions", json={"refresh_token": token})
        keys = list(redis_client.scan_iter("rl:*"))
        assert keys
        for key in keys:
            assert token not in key


class TestLogout:
    def test_logout_revokes_current_session(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        _register(client, "out@example.com")
        tokens = _login(client, "out@example.com").json()["data"]
        resp = _delete_with_body(
            client,
            "/v1/auth/sessions",
            {"refresh_token": tokens["refresh_token"]},
            _auth_header(tokens["access_token"]),
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        user = db_session.scalar(select(User).where(User.email == "out@example.com"))
        assert user is not None
        session = db_session.scalar(
            select(AuthSession).where(
                AuthSession.user_id == user.id,
                AuthSession.refresh_token_hash == refresh_token_hash(tokens["refresh_token"]),
            )
        )
        assert session is not None
        assert session.revoked_at is not None

    def test_repeat_logout_is_idempotent(self, client: TestClient, clean_rate_limit_keys) -> None:
        _register(client, "twice@example.com")
        tokens = _login(client, "twice@example.com").json()["data"]
        headers = _auth_header(tokens["access_token"])
        body = {"refresh_token": tokens["refresh_token"]}
        assert _delete_with_body(client, "/v1/auth/sessions", body, headers).status_code == 200
        assert _delete_with_body(client, "/v1/auth/sessions", body, headers).status_code == 200

    def test_cross_user_logout_rejected(self, client: TestClient, clean_rate_limit_keys) -> None:
        _register(client, "a@example.com")
        _register(client, "b@example.com")
        tokens_a = _login(client, "a@example.com").json()["data"]
        _login(client, "b@example.com")
        resp = _delete_with_body(
            client,
            "/v1/auth/sessions",
            {"refresh_token": tokens_a["refresh_token"]},
            _auth_header(_login(client, "b@example.com").json()["data"]["access_token"]),
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == 10006


class TestAccessTokenValidation:
    def test_me_without_token_10001(self, client: TestClient, clean_rate_limit_keys) -> None:
        resp = client.get("/v1/users/me")
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 10001
        assert body["msg"] == "请重新登录"

    @pytest.mark.parametrize(
        "token",
        [
            "garbage-token",
            "Bearer",
            "",
        ],
    )
    def test_me_with_invalid_token_10001(
        self, client: TestClient, token: str, clean_rate_limit_keys
    ) -> None:
        resp = client.get("/v1/users/me", headers=_auth_header(token))
        assert resp.status_code == 401
        assert resp.json()["code"] == 10001

    def test_expired_token_10001(self, client: TestClient, clean_rate_limit_keys) -> None:
        now = datetime.now(UTC)
        expired = jwt.encode(
            {
                "sub": "00000000-0000-4000-8000-000000000000",
                "iat": now,
                "exp": now,
                "type": "access",
            },
            _JWT_SECRET,
            algorithm="HS256",
        )
        resp = client.get("/v1/users/me", headers=_auth_header(expired))
        assert resp.status_code == 401
        assert resp.json()["code"] == 10001

    def test_refresh_type_token_rejected(self, client: TestClient, clean_rate_limit_keys) -> None:
        now = datetime.now(UTC)
        wrong_type = jwt.encode(
            {
                "sub": "00000000-0000-4000-8000-000000000000",
                "iat": now,
                "exp": now + timedelta(hours=1),
                "type": "refresh",
            },
            _JWT_SECRET,
            algorithm="HS256",
        )
        resp = client.get("/v1/users/me", headers=_auth_header(wrong_type))
        assert resp.status_code == 401
        assert resp.json()["code"] == 10001


class TestProfile:
    def test_get_and_update_own_profile(self, client: TestClient, clean_rate_limit_keys) -> None:
        _register(client, "me@example.com", display_name="原名")
        tokens = _login(client, "me@example.com").json()["data"]
        headers = _auth_header(tokens["access_token"])
        me = client.get("/v1/users/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["data"]["display_name"] == "原名"
        updated = client.patch("/v1/users/me", json={"display_name": "新名"}, headers=headers)
        assert updated.status_code == 200
        assert updated.json()["data"]["display_name"] == "新名"
