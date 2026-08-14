"""限流中间件单元测试（T030）：策略分类、fail-open/fail-closed、无业务副作用。"""

from __future__ import annotations

import os

import redis as redis_lib
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.middleware.errors import register_exception_handlers
from app.api.middleware.rate_limit import RateLimitMiddleware
from app.api.middleware.trace import TraceMiddleware
from app.api.v1.schemas.common import success_response
from app.core.security import create_access_token
from app.infrastructure.rate_limit.config import RateLimitSettings
from app.infrastructure.rate_limit.policies import build_policies
from app.infrastructure.rate_limit.redis_limiter import RateLimitDecision


class _StubLimiter:
    """可编程限流器替身：记录键、可注入 RedisError 或固定拒绝。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self.raise_redis_error = False
        self.deny_after = float("inf")

    def check(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        self.calls.append((key, limit, window_seconds))
        if self.raise_redis_error:
            raise redis_lib.ConnectionError("redis down")
        if len(self.calls) > self.deny_after:
            return RateLimitDecision(allowed=False, retry_after_seconds=7)
        return RateLimitDecision(allowed=True)

    def build_key(self, policy_name: str, window_seconds: int, subject: str) -> str:
        return f"stub:{policy_name}:{window_seconds}:{subject}"


def _user_token(sub: str = "user-1") -> str:
    """用测试环境 JWT 密钥签发真实 Access Token（中间件按解码 sub 计键）。"""
    return create_access_token(sub, os.environ["AUTH_JWT_SECRET_KEY"])


def _make_app(limiter: _StubLimiter, *, read_fail_open: bool = True) -> FastAPI:
    app = FastAPI()
    settings = RateLimitSettings(
        subject_hmac_key=SecretStr("k" * 40), read_fail_open=read_fail_open
    )

    class _SettingsLike:
        rate_limit = settings

    app.add_middleware(
        RateLimitMiddleware,
        settings=_SettingsLike(),  # type: ignore[arg-type]
        limiter=limiter,
        policies=build_policies(settings),
    )
    app.add_middleware(TraceMiddleware)
    register_exception_handlers(app)

    @app.post("/v1/users")
    def register() -> dict:
        return success_response({"created": True}).model_dump(mode="json")

    @app.post("/v1/auth/sessions")
    def login() -> dict:
        return success_response({"logged_in": True}).model_dump(mode="json")

    @app.put("/v1/auth/sessions")
    def refresh() -> dict:
        return success_response({"refreshed": True}).model_dump(mode="json")

    @app.post("/v1/knowledge-bases/{kb_id}/documents")
    def upload(kb_id: str) -> dict:
        return success_response({"uploaded": True}).model_dump(mode="json")

    @app.post("/v1/conversations/{cid}/messages")
    def question(cid: str) -> dict:
        return success_response({"asked": True}).model_dump(mode="json")

    @app.get("/v1/users/me")
    def me() -> dict:
        return success_response({"me": True}).model_dump(mode="json")

    return app


class TestPolicyClassification:
    def test_register_uses_auth_ip_and_account(self) -> None:
        limiter = _StubLimiter()
        client = TestClient(_make_app(limiter))
        client.post("/v1/users", json={"email": "A@Example.COM ", "password": "x" * 8})
        # 两条规则：来源 IP（直连对端）与规范化邮箱账号（HMAC 摘要，不含原邮箱）。
        assert len(limiter.calls) == 2
        subjects = {key.split(":")[-1] for key, _, _ in limiter.calls}
        assert "testclient" in subjects  # TestClient 直连对端
        assert len(subjects) == 2
        for key, _, _ in limiter.calls:
            assert key.startswith("stub:auth-ip-and-account:300:")
            assert "a@example.com" not in key

    def test_refresh_uses_ip_and_token_fingerprint(self) -> None:
        limiter = _StubLimiter()
        client = TestClient(_make_app(limiter))
        token = "rt_" + "A" * 43
        client.put("/v1/auth/sessions", json={"refresh_token": token})
        assert len(limiter.calls) == 2
        # token 原值不得出现在任何键中。
        for key, _, _ in limiter.calls:
            assert token not in key

    def test_upload_uses_user_policy(self) -> None:
        limiter = _StubLimiter()
        client = TestClient(_make_app(limiter))
        token = _user_token()
        client.post(
            "/v1/knowledge-bases/00000000-0000-4000-8000-000000000001/documents",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert len(limiter.calls) == 1
        assert limiter.calls[0][0].startswith("stub:upload-user:")
        # 原始 token 不得出现在键中（键为解码 sub 的 HMAC）。
        assert token not in limiter.calls[0][0]

    def test_question_uses_user_policy(self) -> None:
        limiter = _StubLimiter()
        client = TestClient(_make_app(limiter))
        client.post(
            "/v1/conversations/00000000-0000-4000-8000-000000000002/messages",
            headers={"Authorization": f"Bearer {_user_token()}"},
        )
        assert limiter.calls[0][0].startswith("stub:question-user:")

    def test_default_authenticated_policy(self) -> None:
        limiter = _StubLimiter()
        client = TestClient(_make_app(limiter))
        client.get("/v1/users/me", headers={"Authorization": f"Bearer {_user_token()}"})
        assert limiter.calls[0][0].startswith("stub:authenticated-default:")

    def test_invalid_token_skips_user_rule(self) -> None:
        # 无效 token 无法解码 sub：user 规则跳过，请求继续到路由（认证依赖 401）。
        limiter = _StubLimiter()
        client = TestClient(_make_app(limiter))
        resp = client.get("/v1/users/me", headers={"Authorization": "Bearer bogus-token"})
        assert resp.status_code == 200  # 无主体规则可跳过，路由正常响应
        assert limiter.calls == []

    def test_same_user_rotated_tokens_share_budget(self) -> None:
        # FR-026 “每用户”预算：同一用户的新旧 token 解码出相同 sub → 相同限流键。
        limiter = _StubLimiter()
        client = TestClient(_make_app(limiter))
        token_a = _user_token("user-9")
        token_b = _user_token("user-9")
        client.get("/v1/users/me", headers={"Authorization": f"Bearer {token_a}"})
        client.get("/v1/users/me", headers={"Authorization": f"Bearer {token_b}"})
        assert len(limiter.calls) == 2
        assert limiter.calls[0][0] == limiter.calls[1][0]
        # 不同用户 → 不同键。
        client.get("/v1/users/me", headers={"Authorization": f"Bearer {_user_token('user-8')}"})
        assert limiter.calls[2][0] != limiter.calls[0][0]


class TestFailClosedAndFailOpen:
    def test_state_change_fail_closed_on_redis_error(self) -> None:
        limiter = _StubLimiter()
        limiter.raise_redis_error = True
        client = TestClient(_make_app(limiter, read_fail_open=True))
        resp = client.post("/v1/users", json={"email": "a@b.co", "password": "x" * 8})
        assert resp.status_code == 503
        body = resp.json()
        assert body["code"] == 50001
        assert body["msg"] == "系统繁忙，请稍后再试"
        assert "trace_id" in body

    def test_read_fail_open_on_redis_error(self) -> None:
        limiter = _StubLimiter()
        limiter.raise_redis_error = True
        client = TestClient(_make_app(limiter, read_fail_open=True))
        resp = client.get("/v1/users/me", headers={"Authorization": f"Bearer {_user_token()}"})
        assert resp.status_code == 200

    def test_read_fail_closed_when_configured(self) -> None:
        limiter = _StubLimiter()
        limiter.raise_redis_error = True
        client = TestClient(_make_app(limiter, read_fail_open=False))
        resp = client.get("/v1/users/me", headers={"Authorization": f"Bearer {_user_token()}"})
        assert resp.status_code == 503
        assert resp.json()["code"] == 50001


class TestDeniedResponse:
    def test_429_envelope_with_retry_after(self) -> None:
        limiter = _StubLimiter()
        limiter.deny_after = 0
        client = TestClient(_make_app(limiter))
        resp = client.post("/v1/users", json={"email": "a@b.co", "password": "x" * 8})
        assert resp.status_code == 429
        assert resp.headers["Retry-After"] == "7"
        body = resp.json()
        assert body["code"] == 10005
        assert body["msg"] == "请求过于频繁，请稍后再试"
        assert body["data"] is None
        assert "trace_id" in body

    def test_denied_request_never_reaches_handler(self) -> None:
        # 429 时业务处理器不得执行（无业务副作用）。
        executed: list[str] = []
        app = FastAPI()

        class _S:
            rate_limit = RateLimitSettings(subject_hmac_key=SecretStr("k" * 40))

        limiter = _StubLimiter()
        limiter.deny_after = 0
        app.add_middleware(RateLimitMiddleware, settings=_S(), limiter=limiter)  # type: ignore[arg-type]
        app.add_middleware(TraceMiddleware)

        @app.post("/v1/users")
        def register() -> dict:
            executed.append("register")
            return success_response({}).model_dump(mode="json")

        client = TestClient(app)
        resp = client.post("/v1/users", json={"email": "a@b.co", "password": "x" * 8})
        assert resp.status_code == 429
        assert executed == []
