"""知识库规范化名称唯一性集成测试（T142 / FR-003）。

数据模型要求：当前用户的 active 知识库以 ``trim + Unicode casefold`` 后的名称
为部分唯一键；删除中和删除失败墓碑不占用该名称。冲突必须由数据库约束在并发写入
时裁决，并由 API 映射为 ``20016/409``。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import KnowledgeBaseStatus
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User

pytestmark = pytest.mark.integration


def _register(client: TestClient, email: str) -> dict:
    assert (
        client.post("/v1/users", json={"email": email, "password": "password123"}).status_code
        == 201
    )
    response = client.post("/v1/auth/sessions", json={"email": email, "password": "password123"})
    assert response.status_code == 201
    return response.json()["data"]


def _headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


class TestDeletedKnowledgeBaseNameReuse:
    @pytest.mark.parametrize(
        ("status", "delete_error_code"),
        [
            (KnowledgeBaseStatus.DELETING, None),
            (KnowledgeBaseStatus.DELETE_FAILED, 20015),
        ],
    )
    def test_delete_state_does_not_reserve_normalized_name(
        self,
        client: TestClient,
        db_session: Session,
        clean_rate_limit_keys,
        status: KnowledgeBaseStatus,
        delete_error_code: int | None,
    ) -> None:
        tokens = _register(client, "t142-tombstone@example.com")
        headers = _headers(tokens)
        user = db_session.scalar(select(User).where(User.email == "t142-tombstone@example.com"))
        assert user is not None
        # 先通过创建接口持久化 active 记录，确保其 normalized_name 与候选名称相同；
        # 再模拟删除状态。若夹具直接插入删除态而遗漏 normalized_name，部分唯一索引
        # 是否正确排除删除态无法被验证。
        original = client.post(
            "/v1/knowledge-bases", json={"name": "  Retired Research  "}, headers=headers
        )
        assert original.status_code == 201
        tombstone = db_session.scalar(select(KnowledgeBase).where(KnowledgeBase.user_id == user.id))
        assert tombstone is not None
        tombstone.status = status
        tombstone.delete_error_code = delete_error_code
        db_session.commit()

        response = client.post(
            "/v1/knowledge-bases",
            json={"name": "retired research"},
            headers=headers,
        )
        assert response.status_code == 201


class TestConcurrentNormalizedNameUniqueness:
    def test_concurrent_creates_allow_exactly_one_normalized_name(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "t142-concurrent@example.com")
        headers = _headers(tokens)

        def _create(name: str) -> int:
            # 每个线程独立 TestClient 和请求数据库会话，避免测试客户端共享状态。
            with TestClient(client.app, raise_server_exceptions=False) as parallel:
                return parallel.post(
                    "/v1/knowledge-bases", json={"name": name}, headers=headers
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(_create, ["  Concurrent Lab  ", "concurrent lab"]))

        assert sorted(statuses) == [201, 409]
        user = db_session.scalar(select(User).where(User.email == "t142-concurrent@example.com"))
        assert user is not None
        active_count = db_session.scalar(
            select(func.count())
            .select_from(KnowledgeBase)
            .where(
                KnowledgeBase.user_id == user.id,
                KnowledgeBase.status == KnowledgeBaseStatus.ACTIVE,
            )
        )
        assert active_count == 1
