"""全链路后端门禁集成测试（T087 / quickstart 后端优先验证 1-25 条核心自动化）。

单一门禁文件：通过真实 API（TestClient）覆盖

1. 认证会话撤销：登出撤销 refresh、重放 10006/401、重复登出幂等、跨用户登出拒绝；
2. 邮箱规范化复用：存储/登录/限流统一 casefold 值，同规范化邮箱重复注册 20006/409；
3. 可信代理来源 IP：无可信代理时伪造 X-Forwarded-For 不改限流主体（10005/429），
   配置 RATE_LIMIT_TRUSTED_PROXY_CIDRS 后多跳链由右向左取首个非可信地址；
4. 租户隔离：跨用户知识库 20002/404、跨用户会话/消息/引用 20007/404，
   不存在资源与跨租户响应一致（无全局探测泄露）；
5. 公开读取不可见删除态：资料/KB DELETE 后立即隐藏，status=deleting 过滤 10003/400；
6. 资料删除编排：deleting 重复 DELETE 幂等、20015 墓碑重试递增轮次并保留旧历史、
   deleted 后 404、运行写入 fencing、租约超时扫描器接管、检索排除；
7. 未就绪 pending 不执行：上传批次未转正前任务不可执行且无 attempt；
8. 上传接管/幂等：同键重放返回首次结果，协调窗口过期后由重放接管；
9. 解析安全与阶段编排：损坏 PDF 收敛 20001，正常文件 parse→chunk→embed→finalize
   至 completed 且 chunk_count>0；
10. 处理名额：单用户最多 3 份资料 processing；
11. 检索过滤与相似度门槛拒答：无关问题可信拒答（不调用生成、零引用），
    有证据问题返回引用；旧版本/未完成资料排除；
12. 引用快照：删除被引用资料后历史引用为 snapshot 且保留文件名/定位/内容预览；
13. assistant 三类终态：completed/stop、failed/error、cancelled/cancelled，
    失联 streaming 由维护扫描器收敛 failed/error；
14. 知识库删除编排：立即隐藏、子资料清理后物理删除、delete_failed 墓碑仅属主可见；
15. 可信拒答与 KNOWLEDGE_BASE_NOT_READY：无完成资料提问 20005/409。

复杂内部事务（fencing/名额/扫描器）借助已有服务/仓储/worker 函数驱动（与
tests/integration 既有测试同一技术），保证断言确定性（无 sleep 竞态）。
需要真实 PostgreSQL、Redis 与本地持久卷。
"""

import asyncio
import io
import json
import os
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.types import Receive, Scope, Send

from app.core.settings import get_settings
from app.infrastructure.model_gateway.types import GatewayError, GenerationDelta
from app.models.chunk import Chunk
from app.models.conversation import Conversation, Message, MessageCitation
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import (
    DocumentAttemptStatus,
    DocumentStatus,
    DocumentTaskStatus,
    DocumentTaskType,
    FileType,
    KnowledgeBaseStatus,
    MessageFinishReason,
    MessageRole,
    MessageStatus,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.processing_lease import DocumentProcessingLease
from app.models.user import User
from app.services.answer_service import NO_EVIDENCE_CONTENT, AnswerService
from app.services.conversation_service import ConversationService
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.services.llm.chat import GenerationFailure
from app.services.llm.embeddings import EmbeddingService

pytestmark = pytest.mark.integration

_KB_NOT_FOUND_MSG = "请求的知识库不存在"
_RESOURCE_NOT_FOUND_MSG = "请求的资源不存在"
_RATE_LIMIT_EXCEEDED_MSG = "请求过于频繁，请稍后再试"
_PARSE_FAILED_MSG = "资料解析失败，请删除后重新上传"
_KB_NOT_READY_MSG = "当前知识库没有已完成资料，请上传或等待资料处理完成"

# 测试用默认阈值（与 quickstart / test_rate_limits.py 一致）。
AUTH_IP_LIMIT = 20
AUTH_ACCOUNT_LIMIT = 5

_EMBEDDING_DIM = 1536


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema(test_engine):
    """触发会话级 test_engine 创建 schema：本模块部分测试不注入 db_session。"""
    yield


# ---------------------------------------------------------------------------
# API 辅助
# ---------------------------------------------------------------------------


def _register(client: TestClient, email: str, password: str = "password123") -> dict:
    """注册并登录；返回会话令牌。"""
    resp = client.post("/v1/users", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    login = client.post("/v1/auth/sessions", json={"email": email, "password": password})
    assert login.status_code == 201, login.text
    return login.json()["data"]


def _register_only(client: TestClient, email: str, password: str = "password123"):
    return client.post("/v1/users", json={"email": email, "password": password})


def _login(client: TestClient, email: str, password: str = "password123"):
    return client.post("/v1/auth/sessions", json={"email": email, "password": password})


def _headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _create_kb(client: TestClient, headers: dict, name: str = "kb") -> str:
    resp = client.post(
        "/v1/knowledge-bases", json={"name": name, "description": "说明"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _upload(
    client: TestClient,
    kb_id: str,
    files: list[tuple[str, bytes]],
    headers: dict,
    idempotency_key: str | None = None,
):
    multipart = [("files", (name, content, "application/octet-stream")) for name, content in files]
    extra = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post(
        f"/v1/knowledge-bases/{kb_id}/documents", files=multipart, headers={**headers, **extra}
    )


def _delete_with_body(client: TestClient, url: str, payload: dict, headers: dict):
    # httpx delete 不支持 json 参数；request 方法支持请求体。
    return client.request("DELETE", url, json=payload, headers=headers)


def _dependency_overrides(client: TestClient) -> dict:
    """返回应用的依赖覆盖表（TestClient.app 类型为 ASGIApp，需收敛为 FastAPI）。"""
    return cast(FastAPI, client.app).dependency_overrides


def _parse_sse(raw: bytes) -> list[tuple[str, dict]]:
    """把原始 ``event:``/``data:`` 文本帧解码为 (event, data) 列表。"""
    frames: list[tuple[str, dict]] = []
    for block in raw.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event, data_line = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_line = line[len("data:") :].strip()
        assert event is not None and data_line is not None, f"非法 SSE 帧: {block!r}"
        frames.append((event, json.loads(data_line)))
    return frames


def _uf(name: str, content: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name)


# ---------------------------------------------------------------------------
# 数据播种辅助
# ---------------------------------------------------------------------------


def _seed_document(
    db_session: Session,
    storage: FileStorage,
    dispatch,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    filename: str = "doc.txt",
    content: bytes = b"hello gate",
) -> uuid.UUID:
    """经完整上传服务创建 queued 资料（对象写入给定持久卷）。"""
    from app.services.document_service import DocumentService

    service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
    outcome = service.upload(user_id, kb_id, [_uf(filename, content)])
    return uuid.UUID(outcome.items[0]["id"])


def _cleanup_task(
    db_session: Session, doc_id: uuid.UUID, delete_cycle: int | None = None
) -> DocumentTask:
    """取删除清理任务；默认取最新轮次（重试后会存在多个历史清理任务）。"""
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


def _completed_doc_with_chunks(
    db_session: Session,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    *,
    word: str,
    seqs: tuple[int, ...] = (1, 2),
    version: int = 1,
    embedding: list[float] | None = None,
) -> uuid.UUID:
    """直接播种已完成资料与正式片段（跳过流水线，供检索/引用测试）。"""
    doc = Document(
        user_id=user_id,
        knowledge_base_id=kb_id,
        filename="doc.txt",
        file_type=FileType.TXT,
        file_size=10,
        status=DocumentStatus.COMPLETED,
        version=version,
        storage_path=f"obj/seed/{uuid.uuid4()}",
        upload_batch_id=uuid.uuid4(),
        content_hash="c",
        chunk_count=len(seqs),
    )
    db_session.add(doc)
    db_session.flush()
    for seq in seqs:
        db_session.add(
            Chunk(
                user_id=user_id,
                knowledge_base_id=kb_id,
                document_id=doc.id,
                document_version=version,
                seq=seq,
                content=f"{word} {word} {word}",
                embedding=embedding or [0.0] * _EMBEDDING_DIM,
                embedding_model="text-embedding-3-small",
                policy_version="v1",
                page=1,
                section="s",
            )
        )
    db_session.commit()
    return doc.id


def _seed_conversation(
    db_session: Session,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    *,
    with_citation: bool = False,
    chunk_id: uuid.UUID | None = None,
    doc_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """直接播种会话（可选附带消息/引用）；返回会话 ID。"""
    conversation = Conversation(user_id=user_id, knowledge_base_id=kb_id, title="c")
    db_session.add(conversation)
    db_session.flush()
    if with_citation:
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
                chunk_id=chunk_id,
                document_id=doc_id,
                document_version=1,
                rank=1,
                score=0.9,
                chunk_snapshot={
                    "filename": "doc.txt",
                    "file_type": "txt",
                    "page": 1,
                    "section": "s",
                    "content": "uniquetokenablephrase",
                },
            )
        )
    db_session.commit()
    return conversation.id


def _running_state(
    db_session: Session,
    user_id: uuid.UUID,
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    *,
    lease_expired: bool,
) -> tuple[DocumentTask, DocumentTaskAttempt, DocumentProcessingLease]:
    """资料 processing、任务/attempt running、持有处理租约的失联前状态。"""
    doc = db_session.get(Document, doc_id)
    assert doc is not None
    task = db_session.query(DocumentTask).filter_by(document_id=doc_id).one()
    task.status = DocumentTaskStatus.RUNNING
    doc.status = DocumentStatus.PROCESSING
    doc.current_task_type = task.task_type
    started = datetime.now(UTC) - timedelta(seconds=600)
    lease = DocumentProcessingLease(
        user_id=user_id,
        document_id=doc_id,
        task_id=task.id,
        acquired_at=started,
        heartbeat_at=started,
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=300)
            if lease_expired
            else datetime.now(UTC) + timedelta(seconds=300)
        ),
    )
    db_session.add(lease)
    db_session.flush()
    attempt = DocumentTaskAttempt(
        task_id=task.id,
        user_id=user_id,
        knowledge_base_id=kb_id,
        document_id=doc_id,
        document_version=1,
        attempt_no=1,
        worker_name="lost-worker",
        status=DocumentAttemptStatus.RUNNING,
        started_at=started,
    )
    db_session.add(attempt)
    db_session.commit()
    return task, attempt, lease


class FakeEmbeddings(EmbeddingService):
    """返回与文本数相同、维度 1536 的确定性向量（流水线 embed 阶段替身）。"""

    def __init__(self) -> None:
        # 测试替身不初始化模型网关。
        self.settings = get_settings()

    def embed_texts(self, texts: list[str], **kwargs) -> list[list[float]]:
        return [[0.1] + [0.0] * (_EMBEDDING_DIM - 1) for _ in texts]


# ---------------------------------------------------------------------------
# 1. 认证会话撤销（quickstart 后端优先验证 3/15）
# ---------------------------------------------------------------------------


class TestAuthSessionRevocation:
    def test_logout_revokes_session_and_refresh_returns_10006(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "gate-out@example.com")
        body = {"refresh_token": tokens["refresh_token"]}
        resp = _delete_with_body(client, "/v1/auth/sessions", body, _headers(tokens))
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        # 已撤销 refresh token 刷新 → 10006/401。
        refresh = client.put("/v1/auth/sessions", json=body)
        assert refresh.status_code == 401
        assert refresh.json()["code"] == 10006
        assert refresh.json()["msg"] == "登录状态已失效，请重新登录"

    def test_repeat_logout_is_idempotent(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens = _register(client, "gate-twice@example.com")
        headers = _headers(tokens)
        body = {"refresh_token": tokens["refresh_token"]}
        assert _delete_with_body(client, "/v1/auth/sessions", body, headers).status_code == 200
        # 重复登出已撤销会话仍成功（幂等）。
        assert _delete_with_body(client, "/v1/auth/sessions", body, headers).status_code == 200

    def test_cross_user_logout_10006(self, client: TestClient, clean_rate_limit_keys) -> None:
        tokens_a = _register(client, "gate-a@example.com")
        tokens_b = _register(client, "gate-b@example.com")
        resp = _delete_with_body(
            client,
            "/v1/auth/sessions",
            {"refresh_token": tokens_a["refresh_token"]},
            _headers(tokens_b),
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == 10006


# ---------------------------------------------------------------------------
# 2. 邮箱规范化复用（quickstart 后端优先验证 3）
# ---------------------------------------------------------------------------


class TestEmailNormalization:
    def test_register_normalizes_and_login_reuses_casefolded_email(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        resp = _register_only(client, "  User@Example.COM  ")
        assert resp.status_code == 201
        assert resp.json()["data"]["email"] == "user@example.com"
        user = db_session.query(User).filter_by(email="user@example.com").one()
        assert user.email == "user@example.com"
        # 不同大小写/空格的同一邮箱登录成功。
        for variant in ("USER@example.com", "  user@EXAMPLE.COM", "User@Example.COM "):
            login = _login(client, variant)
            assert login.status_code == 201, variant
        # 相同规范化邮箱重复注册 → 20006/409。
        dup = _register_only(client, "USER@EXAMPLE.com ")
        assert dup.status_code == 409
        assert dup.json()["code"] == 20006
        assert dup.json()["msg"] == "该邮箱已注册，请直接登录"

    def test_rate_limit_account_uses_normalized_email(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        """限流按规范化邮箱计键：大小写/空格变体共享同一账号预算。"""
        email = "  RateCase@Example.COM  "
        _register_only(client, email)  # 消耗 1 次账号预算
        variants = [
            "ratecase@example.com",
            "RATECASE@EXAMPLE.COM ",
            "  RateCase@Example.COM",
            "ratecase@example.com ",
            "RATECASE@example.COM",
        ]
        statuses = []
        for variant in variants:
            statuses.append(_login(client, variant, password="wrong-password").status_code)
        # 前 4 次仍为凭证错误；第 5 次（第 6 个请求）命中账号预算 → 429。
        assert statuses[:4] == [401, 401, 401, 401]
        assert statuses[-1] == 429
        denied = _login(client, "ratecase@example.com", password="wrong-password")
        assert denied.status_code == 429
        body = denied.json()
        assert body["code"] == 10005
        assert denied.headers.get("Retry-After") is not None


# ---------------------------------------------------------------------------
# 3. 可信代理来源 IP（quickstart 后端优先验证 19）
# ---------------------------------------------------------------------------


def _fresh_app() -> FastAPI:
    """按 main.py 的中间件顺序构建全新应用（读取当前缓存后的设置）。"""
    from app.api.middleware.errors import register_exception_handlers
    from app.api.middleware.rate_limit import RateLimitMiddleware
    from app.api.middleware.trace import TraceMiddleware
    from app.api.v1.router import api_router

    app = FastAPI(title="gate-test")
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(TraceMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


class _TrustedPeerMiddleware:
    """把直连对端伪装为可信代理 IP（模拟真实部署中经可信代理到达的场景）。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope["client"] = ("10.0.0.5", 50000)
        await self.app(scope, receive, send)


class TestTrustedProxySourceIp:
    def test_forged_xff_ignored_without_trusted_proxies(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        """默认无可信代理：伪造 X-Forwarded-For 不改变来源 IP（共享预算）。"""
        statuses = []
        for i in range(AUTH_IP_LIMIT + 1):
            resp = client.post(
                "/v1/users",
                json={"email": f"xff-{i}@example.com", "password": "password123"},
                headers={"X-Forwarded-For": f"203.0.113.{i % 250 + 1}"},
            )
            statuses.append(resp.status_code)
        assert statuses[0] == 201
        assert statuses[-1] == 429
        body = client.post(
            "/v1/users",
            json={"email": "xff-deny@example.com", "password": "password123"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        ).json()
        assert body["code"] == 10005
        assert body["msg"] == _RATE_LIMIT_EXCEEDED_MSG

    def test_trusted_proxy_multi_hop_takes_first_untrusted_from_right(
        self, clean_rate_limit_keys
    ) -> None:
        """配置可信代理后：多跳链由右向左取首个非可信地址（API 层行为验证）。

        来源解析只在直连对端为可信代理时信任转发链，因此把对端伪装为
        ``10.0.0.5``（可信 CIDR 10.0.0.0/8）模拟经可信代理到达的部署拓扑。
        """
        os.environ["RATE_LIMIT_TRUSTED_PROXY_CIDRS"] = "10.0.0.0/8"
        try:
            get_settings.cache_clear()
            app = _TrustedPeerMiddleware(_fresh_app())
            with TestClient(app, raise_server_exceptions=False) as client:
                # 同一 XFF 链 → 同一来源 IP（203.0.113.9）→ 第 21 次超限。
                statuses = [
                    client.post(
                        "/v1/users",
                        json={"email": f"proxy-{i}@example.com", "password": "password123"},
                        headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
                    ).status_code
                    for i in range(AUTH_IP_LIMIT + 1)
                ]
                assert statuses[0] == 201
                assert statuses[-1] == 429
                # 不同 XFF 链 → 不同来源 IP（198.51.100.7）→ 独立预算放行。
                other = client.post(
                    "/v1/users",
                    json={"email": "proxy-other@example.com", "password": "password123"},
                    headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"},
                )
                assert other.status_code == 201
        finally:
            os.environ.pop("RATE_LIMIT_TRUSTED_PROXY_CIDRS", None)
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 4. 租户隔离（quickstart 后端优先验证 4）
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_cross_user_kb_and_subresources_invisible(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens_a = _register(client, "gate-owner@example.com")
        headers_a = _headers(tokens_a)
        kb_id = _create_kb(client, headers_a, name="a-only")
        resp = _upload(client, kb_id, [("doc.txt", b"secret")], headers_a)
        assert resp.status_code == 202
        doc_id = resp.json()["data"]["documents"][0]["id"]
        conv_id = _seed_conversation(
            db_session,
            db_session.query(User).filter_by(email="gate-owner@example.com").one().id,
            uuid.UUID(kb_id),
        )

        tokens_b = _register(client, "gate-intruder@example.com")
        headers_b = _headers(tokens_b)
        # 跨用户知识库读/改/删 → 20002/404。
        for method, path, payload in (
            ("GET", f"/v1/knowledge-bases/{kb_id}", None),
            ("PATCH", f"/v1/knowledge-bases/{kb_id}", {"name": "x"}),
            ("DELETE", f"/v1/knowledge-bases/{kb_id}", None),
            ("GET", f"/v1/knowledge-bases/{kb_id}/documents", None),
            ("GET", f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", None),
            ("GET", f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}/tasks", None),
        ):
            resp = client.request(method, path, json=payload, headers=headers_b)
            assert resp.status_code == 404, (method, path)
            assert resp.json()["code"] == 20002, (method, path)
            assert resp.json()["msg"] == _KB_NOT_FOUND_MSG
        # 上传同样 20002。
        resp = _upload(client, kb_id, [("x.txt", b"x")], headers_b)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002

        # 跨用户会话/消息/引用 → 20007/404。
        for method, path, payload in (
            ("GET", f"/v1/conversations/{conv_id}", None),
            ("PATCH", f"/v1/conversations/{conv_id}", {"title": "x"}),
            ("DELETE", f"/v1/conversations/{conv_id}", None),
            ("GET", f"/v1/conversations/{conv_id}/messages", None),
        ):
            resp = client.request(method, path, json=payload, headers=headers_b)
            assert resp.status_code == 404, (method, path)
            assert resp.json()["code"] == 20007, (method, path)
            assert resp.json()["msg"] == _RESOURCE_NOT_FOUND_MSG
        resp = client.post(
            f"/v1/conversations/{conv_id}/messages",
            json={"content": "问题"},
            headers=headers_b,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007

        # 不存在资源响应与跨租户一致（无全局探测泄露）。
        ghost_kb = client.get(f"/v1/knowledge-bases/{uuid.uuid4()}", headers=headers_b)
        assert ghost_kb.status_code == 404
        assert ghost_kb.json()["code"] == 20002
        assert ghost_kb.json()["msg"] == _KB_NOT_FOUND_MSG
        ghost_conv = client.get(f"/v1/conversations/{uuid.uuid4()}", headers=headers_b)
        assert ghost_conv.status_code == 404
        assert ghost_conv.json()["code"] == 20007
        assert ghost_conv.json()["msg"] == _RESOURCE_NOT_FOUND_MSG


# ---------------------------------------------------------------------------
# 5. 公开读取不可见删除态（quickstart 后端优先验证 13）
# ---------------------------------------------------------------------------


class TestDeletedStateInvisible:
    def test_document_delete_hides_immediately(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "gate-hide-doc@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("doc.txt", b"x")], headers)
        doc_id = resp.json()["data"]["documents"][0]["id"]

        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        # 详情立即 404；列表不含该资料。
        detail = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert detail.status_code == 404
        assert detail.json()["code"] == 20007
        listing = client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)
        assert listing.json()["data"]["total"] == 0
        # 内部状态过滤参数 → 10003/400。
        for status in ("deleting", "deleted"):
            resp = client.get(
                f"/v1/knowledge-bases/{kb_id}/documents?status={status}", headers=headers
            )
            assert resp.status_code == 400
            assert resp.json()["code"] == 10003

    def test_kb_deleting_hides_kb_and_subresources(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        dispatch, _ = dispatch_calls
        tokens = _register(client, "gate-hide-kb@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="gate-hide-kb@example.com").one()
        _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id))

        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 200
        # 知识库与子资源立即不可见。
        resp = client.get(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        listing = client.get("/v1/knowledge-bases", headers=headers).json()["data"]["items"]
        assert "kb" not in [item["name"] for item in listing]


# ---------------------------------------------------------------------------
# 6. 资料删除编排（quickstart 后端优先验证 13）
# ---------------------------------------------------------------------------


class TestDocumentDeleteOrchestration:
    def test_repeat_delete_deleting_idempotent_no_new_tasks(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "gate-del-idem@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("doc.txt", b"x")], headers)
        doc_id = resp.json()["data"]["documents"][0]["id"]
        doc_uuid = uuid.UUID(doc_id)

        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()
        doc = db_session.get(Document, doc_uuid)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETING
        assert doc.delete_cycle == 1
        tasks_before = db_session.query(DocumentTask).count()

        # deleting 状态重复 DELETE 幂等成功：轮次与任务数不变。
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()
        doc = db_session.get(Document, doc_uuid)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETING
        assert doc.delete_cycle == 1
        assert db_session.query(DocumentTask).count() == tasks_before

    def test_cleanup_success_deleted_tombstone_404_and_retrieval_excluded(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        from app.repositories.chunks import ChunkRepository
        from app.workers.document_delete_cleanup import process_delete_cleanup

        dispatch, _ = dispatch_calls
        tokens = _register(client, "gate-del-tomb@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="gate-del-tomb@example.com").one()
        doc_id = _completed_doc_with_chunks(
            db_session, user.id, uuid.UUID(kb_id), word="uniquetokenablephrase"
        )
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
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
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.DELETED
        # deleted 后 GET/DELETE 均 404。
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007
        # 检索排除已删除资料。
        assert ChunkRepository(db_session).count_retrievable(user.id, uuid.UUID(kb_id)) == 0

    def test_20015_tombstone_retry_increments_cycle_and_keeps_history(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        from app.workers.document_delete_cleanup import process_delete_cleanup

        dispatch, _ = dispatch_calls
        tokens = _register(client, "gate-del-20015@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="gate-del-20015@example.com").one()
        doc_id = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id))
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200

        class FailingStorage(FileStorage):
            def delete_object(self, object_key: str) -> None:
                raise OSError("disk gone")

        broken = FailingStorage(storage.storage)
        cleanup = _cleanup_task(db_session, doc_id)
        for _round in range(4):  # 初次 + 3 次重试全部失败 → 20015 墓碑
            process_delete_cleanup(
                db_session,
                task_id=cleanup.id,
                user_id=user.id,
                knowledge_base_id=uuid.UUID(kb_id),
                document_id=doc_id,
                document_version=1,
                file_storage=broken,
                dispatch=dispatch,
            )
            db_session.expire_all()
            cleanup = _cleanup_task(db_session, doc_id)
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.FAILED
        assert doc.current_task_type == DocumentTaskType.DELETE_CLEANUP
        assert doc.error_code == 20015
        old_attempts = (
            db_session.query(DocumentTaskAttempt)
            .filter_by(task_id=cleanup.id)
            .order_by(DocumentTaskAttempt.attempt_no)
            .all()
        )
        assert [a.attempt_no for a in old_attempts] == [1, 2, 3, 4]

        # 详情 API：failed/delete_cleanup/20015 最小墓碑，allowed_actions 仅 retry_delete。
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "failed"
        assert data["current_task_type"] == "delete_cleanup"
        assert data["error_code"] == 20015
        assert data["error_message"] == "资料删除未完成，请重试删除"
        assert data["allowed_actions"] == ["retry_delete"]

        # 从 20015 墓碑再次 DELETE：递增轮次、新建任务、旧任务/attempt 保留。
        tasks_before = db_session.query(DocumentTask).count()
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.delete_cycle == 2  # 仅重试 DELETE 递增轮次
        assert doc.error_code is None  # 墓碑失败码已清除
        old_cleanup = _cleanup_task(db_session, doc_id, delete_cycle=1)
        new_cleanup = _cleanup_task(db_session, doc_id)
        assert new_cleanup.id != old_cleanup.id
        assert new_cleanup.delete_cycle == 2
        assert new_cleanup.status == DocumentTaskStatus.QUEUED
        db_session.refresh(old_cleanup)
        assert old_cleanup.status == DocumentTaskStatus.FAILED
        assert old_cleanup.retry_count == 3
        assert db_session.query(DocumentTask).count() == tasks_before + 1

        # 重试清理成功 → deleted。
        process_delete_cleanup(
            db_session,
            task_id=new_cleanup.id,
            user_id=user.id,
            knowledge_base_id=uuid.UUID(kb_id),
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.expire_all()
        assert db_session.get(Document, doc_id).status == DocumentStatus.DELETED  # type: ignore[union-attr]

    def test_running_attempt_fencing_and_scanner_takeover(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        from app.repositories.fencing import FencingError
        from app.services.document_pipeline import DocumentPipelineOrchestrator
        from app.workers.document_delete_cleanup import process_delete_cleanup
        from app.workers.task_recovery import scan_expired_leases

        dispatch, _ = dispatch_calls
        tokens = _register(client, "gate-del-scan@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="gate-del-scan@example.com").one()
        doc_id = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id))
        task, attempt, lease = _running_state(
            db_session, user.id, uuid.UUID(kb_id), doc_id, lease_expired=False
        )
        frozen_expiry = lease.expires_at

        # 运行 attempt + 未过期租约：DELETE 置 deleting 但不提前释放（冻结等待上限）。
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()
        lease = db_session.get(DocumentProcessingLease, lease.id)
        assert lease is not None
        assert lease.released_at is None
        assert lease.expires_at == frozen_expiry  # 心跳/删除不得延长
        cleanup = _cleanup_task(db_session, doc_id)
        assert cleanup.status == DocumentTaskStatus.PENDING  # 等待扫描器接管

        # 删除提交后：旧 attempt 的持久化写入被 fencing 拒绝。
        with pytest.raises(FencingError):
            DocumentPipelineOrchestrator(db_session, dispatch=dispatch).complete_stage(
                attempt_id=attempt.id,
                user_id=user.id,
                knowledge_base_id=uuid.UUID(kb_id),
                document_id=doc_id,
                document_version=1,
            )
        db_session.rollback()

        # 租约到期后：扫描器取消 attempt/task、释放名额并激活 delete_cleanup。
        lease = db_session.get(DocumentProcessingLease, lease.id)
        assert lease is not None
        lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()
        assert scan_expired_leases(db_session, dispatch=dispatch, now=datetime.now(UTC)) == 1
        db_session.refresh(attempt)
        db_session.refresh(task)
        db_session.refresh(cleanup)
        assert attempt.status == DocumentAttemptStatus.CANCELLED
        assert task.status == DocumentTaskStatus.CANCELLED
        assert lease.released_at is not None
        assert cleanup.status == DocumentTaskStatus.QUEUED

        # 清理成功收敛 deleted。
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
        assert db_session.get(Document, doc_id).status == DocumentStatus.DELETED  # type: ignore[union-attr]
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20007


# ---------------------------------------------------------------------------
# 7. 未就绪 pending 不执行（quickstart 后端优先验证 5）
# ---------------------------------------------------------------------------


class TestPendingBatchNotRunnable:
    def test_uploaded_batch_queued_with_zero_attempts(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "gate-pending-api@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("doc.txt", b"hello")], headers)
        assert resp.status_code == 202
        doc_id = resp.json()["data"]["documents"][0]["id"]
        tasks = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}/tasks", headers=headers)
        item = tasks.json()["data"]["items"][0]
        assert item["status"] == "queued"
        assert item["attempts"] == []  # 尚未开始执行

    def test_pending_initial_task_never_runs_before_coordination(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        from app.services.upload_validation import validate_upload_batch
        from app.workers.document_parse import process_parse

        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
        validated = validate_upload_batch([_uf("doc.txt", b"pending")])
        service._persist_batch(user_id, kb_id, validated, uuid.uuid4(), None)
        doc = db_session.query(Document).one()
        task = db_session.query(DocumentTask).one()
        assert doc.status == DocumentStatus.PENDING
        assert task.status == DocumentTaskStatus.PENDING
        assert task.queued_at is None

        # 未转正前调用 worker：不得执行（状态不变、无 attempt、零投递）。
        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc.id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.refresh(doc)
        db_session.refresh(task)
        assert doc.status == DocumentStatus.PENDING
        assert task.status == DocumentTaskStatus.PENDING
        assert db_session.query(DocumentTaskAttempt).count() == 0
        assert calls == []


# ---------------------------------------------------------------------------
# 8. 上传接管/幂等（quickstart 后端优先验证 5/9）
# ---------------------------------------------------------------------------


class TestUploadIdempotencyAndTakeover:
    _KEY = "gate-key-0001"

    def test_replay_same_key_returns_first_result_without_duplicates(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "gate-idem@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        first = _upload(
            client, kb_id, [("doc.txt", b"same content")], headers, idempotency_key=self._KEY
        )
        assert first.status_code == 202
        first_ids = [item["id"] for item in first.json()["data"]["documents"]]
        replay = _upload(
            client, kb_id, [("doc.txt", b"same content")], headers, idempotency_key=self._KEY
        )
        assert replay.status_code == 202
        replay_ids = [item["id"] for item in replay.json()["data"]["documents"]]
        assert replay_ids == first_ids  # 返回首次结果
        assert db_session.query(Document).count() == 1  # 资料数量不增加
        assert db_session.query(DocumentTask).count() == 1

    def test_expired_coordinating_taken_over_by_replay(
        self, db_session: Session, storage: FileStorage, dispatch_calls, user_and_kb
    ) -> None:
        from app.models.upload_request import DocumentUploadRequest
        from app.services.upload_validation import validate_upload_batch

        dispatch, calls = dispatch_calls
        user_id, kb_id = user_and_kb
        service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
        validated = validate_upload_batch([_uf("f0.pdf", b"%PDF-1.4 content")])
        request = service._persist_batch(user_id, kb_id, validated, uuid.uuid4(), self._KEY)
        assert request is not None
        assert request.status.value == "coordinating"
        # 协调窗口内重放：20008 冲突且零副作用。
        from app.api.middleware.errors import ApiError

        with pytest.raises(ApiError) as exc:
            service.upload(
                user_id, kb_id, [_uf("f0.pdf", b"%PDF-1.4 content")], idempotency_key=self._KEY
            )
        assert exc.value.code == 20008
        # 超过 300 秒窗口（手动调旧）后由重放接管：整批 queued。
        request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()
        outcome = service.upload(
            user_id, kb_id, [_uf("f0.pdf", b"%PDF-1.4 content")], idempotency_key=self._KEY
        )
        assert [item["status"] for item in outcome.items] == ["queued"]
        fresh = db_session.get(DocumentUploadRequest, request.id)
        assert fresh is not None
        assert fresh.status.value == "accepted"
        doc = db_session.query(Document).one()
        assert doc.status == DocumentStatus.QUEUED
        assert db_session.query(DocumentTask).one().status == DocumentTaskStatus.QUEUED
        assert len(calls) == 1  # 接管仅投递一次 parse


# ---------------------------------------------------------------------------
# 9. 解析安全与阶段编排（quickstart 后端优先验证 7/8/11）
# ---------------------------------------------------------------------------


class TestParseSecurityAndPipeline:
    def _drive_pipeline(
        self,
        db_session: Session,
        doc_id: uuid.UUID,
        user_id: uuid.UUID,
        kb_id: uuid.UUID,
        dispatch: Callable,
    ) -> None:
        """parse → chunk → embed → finalize 完整驱动（对象走默认持久卷）。"""
        from app.workers.document_chunk import process_chunk
        from app.workers.document_embed import process_embed
        from app.workers.document_finalize import process_finalize
        from app.workers.document_parse import process_parse

        parse_task = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type=DocumentTaskType.PARSE)
            .one()
        )
        process_parse(
            db_session,
            task_id=parse_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            dispatch=dispatch,
        )
        chunk_task = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type=DocumentTaskType.CHUNK)
            .one()
        )
        process_chunk(
            db_session,
            task_id=chunk_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            dispatch=dispatch,
        )
        embed_task = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type=DocumentTaskType.EMBED)
            .one()
        )
        process_embed(
            db_session,
            task_id=embed_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            embeddings=FakeEmbeddings(),
            dispatch=dispatch,
        )
        finalize_task = (
            db_session.query(DocumentTask)
            .filter_by(document_id=doc_id, task_type=DocumentTaskType.FINALIZE)
            .one()
        )
        process_finalize(
            db_session,
            task_id=finalize_task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            dispatch=dispatch,
        )

    def test_corrupted_pdf_failed_20001_with_fixed_message(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        from app.workers.document_parse import process_parse

        tokens = _register(client, "gate-badpdf@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("broken.pdf", b"%PDF-1.4 this is not a pdf")], headers)
        assert resp.status_code == 202
        doc_id = resp.json()["data"]["documents"][0]["id"]
        doc_uuid = uuid.UUID(doc_id)
        user = db_session.query(User).filter_by(email="gate-badpdf@example.com").one()
        task = db_session.query(DocumentTask).filter_by(document_id=doc_uuid).one()
        process_parse(
            db_session,
            task_id=task.id,
            user_id=user.id,
            knowledge_base_id=uuid.UUID(kb_id),
            document_id=doc_uuid,
            document_version=1,
            dispatch=lambda name, args: None,
        )
        db_session.expire_all()
        # 异步失败不伪装成上传阶段 HTTP 400：详情 HTTP 200 + status=failed/20001。
        detail = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert detail.status_code == 200
        data = detail.json()["data"]
        assert data["status"] == "failed"
        assert data["error_code"] == 20001
        assert data["error_message"] == _PARSE_FAILED_MSG

    def test_normal_file_full_pipeline_completed_with_chunks(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        def noop_dispatch(name: str, args: tuple) -> None:
            return None

        dispatch = noop_dispatch
        tokens = _register(client, "gate-pipe@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = _upload(client, kb_id, [("doc.txt", ("hello pipeline " * 50).encode())], headers)
        doc_id = uuid.UUID(resp.json()["data"]["documents"][0]["id"])
        user = db_session.query(User).filter_by(email="gate-pipe@example.com").one()
        self._drive_pipeline(db_session, doc_id, user.id, uuid.UUID(kb_id), dispatch)
        db_session.expire_all()
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        assert doc.status == DocumentStatus.COMPLETED
        assert doc.current_task_type is None
        assert doc.chunk_count > 0
        detail = client.get(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert detail.status_code == 200
        data = detail.json()["data"]
        assert data["status"] == "completed"
        assert data["chunk_count"] > 0


# ---------------------------------------------------------------------------
# 10. 处理名额（quickstart 后端优先验证 10）
# ---------------------------------------------------------------------------


class TestProcessingSlots:
    MAX_PER_USER = 3

    def test_at_most_max_per_user_processing(
        self,
        db_session: Session,
        test_engine,
        storage: FileStorage,
        dispatch_calls,
        user_and_kb,
    ) -> None:
        from sqlalchemy.orm import sessionmaker

        from app.workers.document_parse import process_parse

        dispatch, _ = dispatch_calls
        user_id, kb_id = user_and_kb
        doc_ids = [
            _seed_document(
                db_session, storage, dispatch, user_id, kb_id, content=f"text {i}".encode()
            )
            for i in range(4)
        ]
        factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
        processing: list[uuid.UUID] = []
        for doc_id in doc_ids:
            s = factory()
            try:
                task = s.query(DocumentTask).filter_by(document_id=doc_id).one()
                process_parse(
                    s,
                    task_id=task.id,
                    user_id=user_id,
                    knowledge_base_id=kb_id,
                    document_id=doc_id,
                    document_version=1,
                    file_storage=storage,
                    dispatch=dispatch,
                )
                s.commit()
                fresh = s.get(Document, doc_id)
                if fresh is not None and fresh.status == DocumentStatus.PROCESSING:
                    processing.append(doc_id)
            finally:
                s.close()
        # 最多 3 份同时 processing；第 4 份保持 queued。
        assert len(processing) == self.MAX_PER_USER
        wait_doc = next(d for d in doc_ids if d not in processing)
        waiting = db_session.get(Document, wait_doc)
        assert waiting is not None
        assert waiting.status == DocumentStatus.QUEUED
        open_leases = (
            db_session.query(DocumentProcessingLease)
            .filter(
                DocumentProcessingLease.user_id == user_id,
                DocumentProcessingLease.released_at.is_(None),
            )
            .count()
        )
        assert open_leases == self.MAX_PER_USER


# ---------------------------------------------------------------------------
# 11. 检索过滤与相似度门槛拒答（quickstart 后端优先验证 12/13/23）
# ---------------------------------------------------------------------------


class _QueryAwareEmbeddings(EmbeddingService):
    """按查询文本返回确定性向量：含“香蕉”→ [0,1]，含“芒果”→ [1,0]，否则低相关。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    def embed_texts(self, texts: list[str], **kwargs) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * _EMBEDDING_DIM
            if "香蕉" in text:
                vec[1] = 1.0
            elif "芒果" in text:
                vec[0] = 1.0
            else:
                vec[2] = 1.0  # 与两路证据正交：余弦 0 < 0.65 门槛 → 向量路排除
            out.append(vec)
        return out


class FakeRewrite:
    def rewrite(self, *, user_id, query, history) -> str:
        return query


class FakeGeneration:
    def __init__(self, deltas: list[str] | None = None, error: GatewayError | None = None) -> None:
        self.deltas = deltas if deltas is not None else ["根据", "资料"]
        self.error = error
        self.calls = 0

    def stream(self, *, user_id, query, context_pack, history):
        self.calls += 1
        if self.error is not None:
            raise GenerationFailure() from self.error
        for text in self.deltas:
            yield GenerationDelta(text=text)
        yield GenerationDelta(text="", finish_reason="stop")


class TestRetrievalEvidenceAndRefusal:
    """真实检索（双路召回 + 门槛 + RRF）+ 假嵌入/生成端口，经 SSE 端点验证。"""

    def _install_answer(
        self, client: TestClient, db_session: Session, user: User
    ) -> FakeGeneration:
        from app.api.v1.dependencies.auth import get_current_user
        from app.api.v1.routes.messages import get_message_answer_service
        from app.infrastructure.database.session import get_db
        from app.services.citation_service import CitationService
        from app.services.conversation_service import ConversationService
        from app.services.retrieval_service import RetrievalService

        generation = FakeGeneration()
        service = AnswerService(
            conversations=ConversationService(db_session),
            retrieval=RetrievalService(
                db_session,
                embedding_service=_QueryAwareEmbeddings(),
                reranker=None,
            ),
            rewrite=FakeRewrite(),
            generation=generation,
            citations=CitationService(db_session),
        )
        _dependency_overrides(client)[get_db] = lambda: db_session
        _dependency_overrides(client)[get_current_user] = lambda: user
        _dependency_overrides(client)[get_message_answer_service] = lambda: service
        return generation

    def test_unrelated_question_refused_without_generation_or_citations(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "gate-retr@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="gate-retr@example.com").one()
        # 完成资料为证据；旧版本/未完成资料排除。
        # 注意：片段向量必须非零且方向可区分——pgvector 对零向量余弦距离取 0（相似度 1）。
        banana_vec = [0.0, 1.0] + [0.0] * (_EMBEDDING_DIM - 2)
        mango_vec = [1.0, 0.0] + [0.0] * (_EMBEDDING_DIM - 2)
        banana_doc = _completed_doc_with_chunks(
            db_session,
            user.id,
            uuid.UUID(kb_id),
            word="香蕉产地泰国",
            embedding=banana_vec,
            seqs=(1,),
        )
        _completed_doc_with_chunks(
            db_session, user.id, uuid.UUID(kb_id), word="芒果产地海南", embedding=mango_vec
        )
        old_doc = _completed_doc_with_chunks(
            db_session, user.id, uuid.UUID(kb_id), word="旧版本内容", version=2
        )
        # 清除助手播种的“当前版本”片段：资料仅保留 version=1 的旧片段（不可检索）。
        db_session.query(Chunk).filter_by(document_id=old_doc).delete()
        db_session.flush()
        db_session.add(
            Chunk(
                user_id=user.id,
                knowledge_base_id=uuid.UUID(kb_id),
                document_id=old_doc,
                document_version=1,  # 旧版本片段
                seq=1,
                content="旧版本内容",
                embedding=mango_vec,
                embedding_model="text-embedding-3-small",
                policy_version="v1",
            )
        )
        queued = Document(
            user_id=user.id,
            knowledge_base_id=uuid.UUID(kb_id),
            filename="queued.txt",
            file_type=FileType.TXT,
            file_size=10,
            storage_path=f"obj/seed/{uuid.uuid4()}",
            upload_batch_id=uuid.uuid4(),
            content_hash="c",
            status=DocumentStatus.QUEUED,
        )
        db_session.add(queued)
        db_session.flush()
        db_session.add(
            Chunk(
                user_id=user.id,
                knowledge_base_id=uuid.UUID(kb_id),
                document_id=queued.id,
                document_version=1,
                seq=1,
                content="未完成资料内容",
                embedding=[1.0] + [0.0] * (_EMBEDDING_DIM - 1),
                embedding_model="text-embedding-3-small",
                policy_version="v1",
            )
        )
        db_session.commit()
        resp = client.post("/v1/conversations", json={"knowledge_base_id": kb_id}, headers=headers)
        conv_id = resp.json()["data"]["id"]
        generation = self._install_answer(client, db_session, user)
        try:
            # 无关问题：无候选 → 可信拒答 completed/stop，不调用生成、不创建引用。
            with client.stream(
                "POST",
                f"/v1/conversations/{conv_id}/messages",
                json={"content": "量子力学原理"},
                headers=headers,
            ) as stream:
                assert stream.status_code == 200
                raw = b"".join(stream.iter_bytes())
            events = _parse_sse(raw)
            names = [name for name, _ in events]
            assert names == ["message_start", "retrieval_done", "message_end"]
            assert events[1][1]["data"]["citations"] == []
            assert events[2][1]["data"]["finish_reason"] == "stop"
            assert generation.calls == 0
            assistant = (
                db_session.query(Message)
                .filter_by(conversation_id=uuid.UUID(conv_id))
                .order_by(Message.created_at.desc())
                .first()
            )
            assert assistant is not None
            assert assistant.status == MessageStatus.COMPLETED
            assert assistant.finish_reason == MessageFinishReason.STOP
            assert assistant.content == NO_EVIDENCE_CONTENT
            assert db_session.query(MessageCitation).count() == 0

            # 有证据问题：命中完成资料（香蕉）→ 引用 + completed/stop；
            # 旧版本与未完成资料不参与召回。
            with client.stream(
                "POST",
                f"/v1/conversations/{conv_id}/messages",
                json={"content": "香蕉价格"},
                headers=headers,
            ) as stream:
                raw = b"".join(stream.iter_bytes())
            events = _parse_sse(raw)
            names = [name for name, _ in events]
            assert names == ["message_start", "retrieval_done", "delta", "delta", "message_end"]
            assert events[2][1]["data"]["text"] == "根据"
            assert events[4][1]["data"]["finish_reason"] == "stop"
            citations = events[1][1]["data"]["citations"]
            assert len(citations) == 1
            assert uuid.UUID(citations[0]["document_id"]) == banana_doc
            assert generation.calls == 1
            assert db_session.query(MessageCitation).count() == 1
        finally:
            _dependency_overrides(client).clear()

    def test_knowledge_base_not_ready_20005(
        self, client: TestClient, clean_rate_limit_keys
    ) -> None:
        tokens = _register(client, "gate-notready@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        resp = client.post("/v1/conversations", json={"knowledge_base_id": kb_id}, headers=headers)
        conv_id = resp.json()["data"]["id"]
        resp = client.post(
            f"/v1/conversations/{conv_id}/messages",
            json={"content": "问题"},
            headers=headers,
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == 20005
        assert body["msg"] == _KB_NOT_READY_MSG
        assert body["data"] is None


# ---------------------------------------------------------------------------
# 12. 引用快照（quickstart 后端优先验证 13/23）
# ---------------------------------------------------------------------------


class TestCitationSnapshot:
    def test_deleted_source_becomes_snapshot_citation(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        from app.workers.document_delete_cleanup import process_delete_cleanup

        dispatch, _ = dispatch_calls
        tokens = _register(client, "gate-snap@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="gate-snap@example.com").one()
        doc_id = _completed_doc_with_chunks(
            db_session, user.id, uuid.UUID(kb_id), word="uniquetokenablephrase"
        )
        chunk = db_session.query(Chunk).filter_by(document_id=doc_id).first()
        assert chunk is not None
        conv_id = _seed_conversation(
            db_session,
            user.id,
            uuid.UUID(kb_id),
            with_citation=True,
            chunk_id=chunk.id,
            doc_id=doc_id,
        )

        # 删除被引用资料并完成清理。
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}/documents/{doc_id}", headers=headers)
        assert resp.status_code == 200
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
        # 历史引用保留：外键置空、source_type=snapshot、文件名/定位/预览保留。
        resp = client.get(
            f"/v1/conversations/{conv_id}/messages",
            headers=headers,
        )
        messages = resp.json()["data"]["items"]
        assistant_id = next(m["id"] for m in messages if m["role"] == "assistant")
        resp = client.get(
            f"/v1/conversations/{conv_id}/messages/{assistant_id}/citations",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        citation = data["items"][0]
        assert citation["source_type"] == "snapshot"
        assert citation["chunk_id"] is None
        assert citation["document_id"] is None
        assert citation["document_version"] == 1
        assert citation["filename"] == "doc.txt"
        assert citation["file_type"] == "txt"
        assert citation["page"] == 1
        assert citation["section"] == "s"
        assert citation["content"] == "uniquetokenablephrase"
        assert citation["rank"] == 1


# ---------------------------------------------------------------------------
# 13. assistant 三类终态（quickstart 后端优先验证 14）
# ---------------------------------------------------------------------------


class _FakeRetrieval:
    def __init__(self, *, retrievable: int = 1) -> None:
        self.retrievable = retrievable

    def count_retrievable(self, user_id, knowledge_base_id) -> int:
        return self.retrievable

    def retrieve(self, user_id, knowledge_base_id, query, *, trace_id=None):
        from app.repositories.chunks import RetrievalChunk
        from app.services.retrieval_service import RetrievalResult

        chunk = RetrievalChunk(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            document_version=1,
            seq=0,
            content="来源内容预览",
            filename="source.txt",
            file_type="txt",
            fused_score=0.9,
        )
        return RetrievalResult(query=query, candidates=(chunk,), context_pack="来源内容预览")


class TestAssistantTerminalStates:
    """正常 completed/stop；生成失败 failed/error；断开 cancelled/cancelled；
    失联 streaming 由维护扫描器收敛 failed/error。"""

    def _sse_setup(
        self, client: TestClient, db_session: Session, email: str
    ) -> tuple[dict, str, User, uuid.UUID]:
        tokens = _register(client, email)
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email=email).one()
        resp = client.post("/v1/conversations", json={"knowledge_base_id": kb_id}, headers=headers)
        conv_id = resp.json()["data"]["id"]
        return headers, conv_id, user, uuid.UUID(kb_id)

    def _install(
        self,
        client: TestClient,
        db_session: Session,
        user: User,
        generation: FakeGeneration,
        retrievable: int = 1,
    ) -> None:
        from app.api.v1.dependencies.auth import get_current_user
        from app.api.v1.routes.messages import get_message_answer_service
        from app.infrastructure.database.session import get_db
        from app.services.conversation_service import ConversationService

        service = AnswerService(
            conversations=ConversationService(db_session),
            retrieval=_FakeRetrieval(retrievable=retrievable),
            rewrite=FakeRewrite(),
            generation=generation,
        )
        _dependency_overrides(client)[get_db] = lambda: db_session
        _dependency_overrides(client)[get_current_user] = lambda: user
        _dependency_overrides(client)[get_message_answer_service] = lambda: service

    def test_success_flow_completed_stop(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        headers, conv_id, user, _kb_id = self._sse_setup(
            client, db_session, "gate-sset@example.com"
        )
        generation = FakeGeneration()
        self._install(client, db_session, user, generation)
        try:
            with client.stream(
                "POST",
                f"/v1/conversations/{conv_id}/messages",
                json={"content": "问题"},
                headers=headers,
            ) as resp:
                raw = b"".join(resp.iter_bytes())
            events = _parse_sse(raw)
            assert [name for name, _ in events] == [
                "message_start",
                "retrieval_done",
                "delta",
                "delta",
                "message_end",
            ]
            assert events[-1][1]["data"]["finish_reason"] == "stop"
            assistant = (
                db_session.query(Message)
                .filter_by(conversation_id=uuid.UUID(conv_id))
                .order_by(Message.created_at.desc())
                .first()
            )
            assert assistant is not None
            assert assistant.status == MessageStatus.COMPLETED
            assert assistant.finish_reason == MessageFinishReason.STOP
            assert assistant.content == "根据资料"
        finally:
            _dependency_overrides(client).clear()

    def test_generation_failure_failed_error(
        self, client: TestClient, db_session: Session, clean_rate_limit_keys
    ) -> None:
        headers, conv_id, user, _kb_id = self._sse_setup(
            client, db_session, "gate-ssef@example.com"
        )
        generation = FakeGeneration(error=GatewayError("provider_error", "provider failed"))
        self._install(client, db_session, user, generation)
        try:
            with client.stream(
                "POST",
                f"/v1/conversations/{conv_id}/messages",
                json={"content": "问题"},
                headers=headers,
            ) as resp:
                raw = b"".join(resp.iter_bytes())
            events = _parse_sse(raw)
            assert [name for name, _ in events] == ["message_start", "retrieval_done", "error"]
            error_frame = events[-1][1]
            assert error_frame["code"] == 50000
            assert error_frame["msg"] == "系统繁忙，请稍后再试"
            assistant = (
                db_session.query(Message)
                .filter_by(conversation_id=uuid.UUID(conv_id))
                .order_by(Message.created_at.desc())
                .first()
            )
            assert assistant is not None
            assert assistant.status == MessageStatus.FAILED
            assert assistant.finish_reason == MessageFinishReason.ERROR
        finally:
            _dependency_overrides(client).clear()

    def test_client_disconnect_cancelled_cancelled(self, db_session: Session) -> None:
        """TestClient 无法模拟流式中断（传输层阻塞不取消应用任务），
        与 test_sse_terminal_states.py 一致：直接 aclose() 异步生成器模拟断开。"""
        from app.api.v1.sse.message_stream import stream_answer_events

        user = User(email="gate-ssec@example.com", password_hash="x" * 60)
        db_session.add(user)
        db_session.flush()
        kb = KnowledgeBase(user_id=user.id, name="kb")
        db_session.add(kb)
        db_session.flush()
        conv = Conversation(user_id=user.id, knowledge_base_id=kb.id)
        db_session.add(conv)
        db_session.commit()

        class BlockingGeneration:
            def stream(self, *, user_id, query, context_pack, history):
                yield GenerationDelta(text="根据")
                import time

                time.sleep(3600)  # 生成中途阻塞，制造断开窗口

        answer = AnswerService(
            conversations=ConversationService(db_session),
            retrieval=_FakeRetrieval(),
            rewrite=FakeRewrite(),
            generation=BlockingGeneration(),
        )
        bundle = answer.prepare(
            user_id=user.id,
            knowledge_base_id=kb.id,
            conversation_id=conv.id,
            content="问题",
        )
        assert bundle.no_evidence is False

        async def _drive() -> None:
            stream = cast(AsyncGenerator[str, None], stream_answer_events(answer, bundle))
            first = await stream.__anext__()
            assert "message_start" in first
            second = await stream.__anext__()
            assert "retrieval_done" in second
            third = await stream.__anext__()
            assert "delta" in third
            # 生成中途客户端断开：关闭异步生成器 → finally 收敛 cancelled/cancelled。
            await stream.aclose()

        asyncio.run(_drive())
        assistant = (
            db_session.query(Message)
            .filter_by(conversation_id=conv.id)
            .order_by(Message.created_at.desc())
            .first()
        )
        assert assistant is not None
        assert assistant.status == MessageStatus.CANCELLED
        assert assistant.finish_reason == MessageFinishReason.CANCELLED

    def test_stale_streaming_converged_by_maintenance_scanner(
        self, db_session: Session, storage: FileStorage, dispatch_calls
    ) -> None:
        from app.workers.task_recovery import run_maintenance_scan

        dispatch, _ = dispatch_calls
        user = User(email="gate-ssescan@example.com", password_hash="x" * 60)
        db_session.add(user)
        db_session.flush()
        kb = KnowledgeBase(user_id=user.id, name="kb")
        db_session.add(kb)
        db_session.flush()
        conv = Conversation(user_id=user.id, knowledge_base_id=kb.id)
        db_session.add(conv)
        db_session.flush()
        stale = Message(
            user_id=user.id,
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            finish_reason=None,
            content="x",
            created_at=datetime.now(UTC) - timedelta(seconds=120),
        )
        db_session.add(stale)
        db_session.commit()

        # 调小失联阈值 + 已调旧消息 → 维护扫描器收敛 failed/error。
        os.environ["MESSAGE_STREAMING_STALE_SECONDS"] = "60"
        try:
            get_settings.cache_clear()
            run_maintenance_scan(db_session, storage=storage, dispatch=dispatch)
        finally:
            os.environ.pop("MESSAGE_STREAMING_STALE_SECONDS", None)
            get_settings.cache_clear()
        db_session.refresh(stale)
        assert stale.status == MessageStatus.FAILED
        assert stale.finish_reason == MessageFinishReason.ERROR


# ---------------------------------------------------------------------------
# 14. 知识库删除编排（quickstart 后端优先验证 25）
# ---------------------------------------------------------------------------


class TestKnowledgeBaseDeleteOrchestration:
    def test_non_empty_kb_delete_hides_and_physically_removes_after_cleanup(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        from app.workers.document_delete_cleanup import process_delete_cleanup
        from app.workers.task_recovery import scan_knowledge_base_deletions

        dispatch, _ = dispatch_calls
        tokens = _register(client, "gate-kbdel@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="gate-kbdel@example.com").one()
        doc_a = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id), "a.txt")
        doc_b = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id), "b.md")
        _seed_conversation(db_session, user.id, uuid.UUID(kb_id))

        # 删除含资料的活跃知识库：提交后立即隐藏。
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 200
        db_session.expire_all()
        kb = db_session.get(KnowledgeBase, uuid.UUID(kb_id))
        assert kb is not None
        assert kb.status == KnowledgeBaseStatus.DELETING
        for doc_id in (doc_a, doc_b):
            doc = db_session.get(Document, doc_id)
            assert doc is not None
            assert doc.status == DocumentStatus.DELETING
            assert doc.delete_cycle == 1
        resp = client.get(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        # 命中 deleting：重复 DELETE 幂等成功且任务数不变。
        tasks_before = db_session.query(DocumentTask).count()
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 200
        assert db_session.query(DocumentTask).count() == tasks_before

        # 子资料清理完成后物理删除（级联对话/消息/引用）；之后 DELETE 404。
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
        assert scan_knowledge_base_deletions(db_session) == 1
        db_session.expire_all()
        assert db_session.get(KnowledgeBase, uuid.UUID(kb_id)) is None
        assert db_session.query(Conversation).count() == 0
        resp = client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002

    def test_delete_failed_tombstone_owner_only_with_retry_delete(
        self,
        client: TestClient,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
        clean_rate_limit_keys,
    ) -> None:
        from app.workers.document_delete_cleanup import process_delete_cleanup
        from app.workers.task_recovery import scan_knowledge_base_deletions

        dispatch, _ = dispatch_calls
        tokens = _register(client, "gate-kbtomb@example.com")
        headers = _headers(tokens)
        kb_id = _create_kb(client, headers)
        user = db_session.query(User).filter_by(email="gate-kbtomb@example.com").one()
        doc_id = _seed_document(db_session, storage, dispatch, user.id, uuid.UUID(kb_id))
        assert client.delete(f"/v1/knowledge-bases/{kb_id}", headers=headers).status_code == 200
        db_session.expire_all()

        class FailingStorage(FileStorage):
            def delete_object(self, object_key: str) -> None:
                raise OSError("disk gone")

        broken = FailingStorage(storage.storage)
        cleanup = _cleanup_task(db_session, doc_id)
        for _round in range(4):  # 清理重试耗尽 → 20015
            process_delete_cleanup(
                db_session,
                task_id=cleanup.id,
                user_id=user.id,
                knowledge_base_id=uuid.UUID(kb_id),
                document_id=doc_id,
                document_version=1,
                file_storage=broken,
                dispatch=dispatch,
            )
            db_session.expire_all()
            cleanup = _cleanup_task(db_session, doc_id)
        assert scan_knowledge_base_deletions(db_session) == 1
        db_session.expire_all()
        kb = db_session.get(KnowledgeBase, uuid.UUID(kb_id))
        assert kb is not None
        assert kb.status == KnowledgeBaseStatus.DELETE_FAILED
        assert kb.delete_error_code == 20015

        # 最小墓碑仅属主可见：allowed_actions=retry_delete，不含名称/子资源。
        items = client.get("/v1/knowledge-bases", headers=headers).json()["data"]["items"]
        assert len(items) == 1
        tomb = items[0]
        assert tomb["status"] == "delete_failed"
        assert tomb["name"] is None
        assert tomb["delete_error_code"] == 20015
        assert tomb["allowed_actions"] == ["retry_delete"]
        resp = client.get(f"/v1/knowledge-bases/{kb_id}/documents", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
        # 他人不可见。
        other = _register(client, "gate-kbtomb-other@example.com")
        other_listing = client.get("/v1/knowledge-bases", headers=_headers(other))
        assert other_listing.json()["data"]["total"] == 0
        resp = client.get(f"/v1/knowledge-bases/{kb_id}", headers=_headers(other))
        assert resp.status_code == 404
        assert resp.json()["code"] == 20002
