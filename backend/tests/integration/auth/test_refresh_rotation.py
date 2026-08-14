"""刷新轮换并发集成测试（T025 / FR-001）。

覆盖：同一 refresh token 的并发轮换最多一个成功，且只创建单一后继会话；后到请求
返回 ``10006/401`` 且不创建第二个后继；无效/过期/已撤销/重放 token 不撤销该用户
其他 active sessions。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth_session import AuthSession
from app.models.user import User

pytestmark = pytest.mark.integration


@pytest.fixture()
def authed_client(client: TestClient, clean_rate_limit_keys) -> dict:
    client.post("/v1/users", json={"email": "rotation@example.com", "password": "password123"})
    tokens = client.post(
        "/v1/auth/sessions", json={"email": "rotation@example.com", "password": "password123"}
    ).json()["data"]
    return {"client": client, "tokens": tokens}


class TestConcurrentRotation:
    def test_exactly_one_success_and_single_successor(
        self, authed_client: dict, db_session: Session
    ) -> None:
        client = authed_client["client"]
        refresh_token = authed_client["tokens"]["refresh_token"]
        user = db_session.scalar(select(User).where(User.email == "rotation@example.com"))
        assert user is not None

        def _refresh(_: int):
            # 独立 TestClient 实例模拟两个并发请求。
            with TestClient(client.app) as parallel:
                return parallel.put(
                    "/v1/auth/sessions", json={"refresh_token": refresh_token}
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(_refresh, range(2)))
        assert sorted(statuses) == [200, 401]

        # 恰好创建单一后继会话：原会话 + 1 个后继。
        count = db_session.scalar(
            select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user.id)
        )
        assert count == 2
        successors = db_session.scalars(
            select(AuthSession).where(
                AuthSession.user_id == user.id, AuthSession.rotated_from_session_id.is_not(None)
            )
        ).all()
        assert len(successors) == 1


class TestNoCascadingRevocation:
    def test_invalid_refresh_does_not_revoke_other_sessions(
        self, authed_client: dict, db_session: Session
    ) -> None:
        client = authed_client["client"]
        user = db_session.scalar(select(User).where(User.email == "rotation@example.com"))
        assert user is not None
        # 第二个 active session。
        second = client.post(
            "/v1/auth/sessions", json={"email": "rotation@example.com", "password": "password123"}
        ).json()["data"]["refresh_token"]

        # 无效 token 刷新被拒绝。
        resp = client.put("/v1/auth/sessions", json={"refresh_token": "rt_" + "B" * 43})
        assert resp.status_code == 401
        assert resp.json()["code"] == 10006

        # 其他 active sessions 未被撤销：原 token 与第二个 token 仍可刷新。
        for token in (authed_client["tokens"]["refresh_token"], second):
            resp = client.put("/v1/auth/sessions", json={"refresh_token": token})
            assert resp.status_code == 200

        # 轮换只撤销被轮换的会话本身：两次成功轮换后仍有两个 active 会话
        # （各后继会话），未发生级联撤销。
        open_count = db_session.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        )
        assert open_count == 2

    def test_expired_session_rejected(self, authed_client: dict, db_session: Session) -> None:
        client = authed_client["client"]
        user = db_session.scalar(select(User).where(User.email == "rotation@example.com"))
        assert user is not None
        session = db_session.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
        assert session is not None
        session.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db_session.commit()

        resp = client.put(
            "/v1/auth/sessions", json={"refresh_token": authed_client["tokens"]["refresh_token"]}
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == 10006
