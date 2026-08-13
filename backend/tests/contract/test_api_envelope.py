"""统一响应、trace_id 与非 SSE 错误信封契约测试（T012）。

契约来源（决策 7 / quickstart 后端优先验证 2、24）：
- 除 SSE 外所有响应均为 ``{code, data, msg, trace_id}`` JSON 信封，成功 ``code=0``；
- ``trace_id`` 为 UUID；客户端可通过 ``X-Trace-Id`` 请求头透传，非法值忽略并重新生成；
  响应头 ``X-Trace-Id`` 回写同一值；
- 校验失败 ``10003/400``、未知路径 ``20007/404``、未分类服务端异常 ``50000/500``，
  所有错误分支均返回统一信封且不泄露异常详情。
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.middleware.errors import ApiError, register_exception_handlers
from app.api.middleware.trace import TraceMiddleware
from app.api.v1.schemas.common import success_response

pytestmark = pytest.mark.contract

KNOWN_TRACE_ID = "7eb23f43-e1f4-4a67-a64d-1a481b36030f"
# 同一 UUID 的非规范书写：无连字符、大写；规范化后必须等于 KNOWN_TRACE_ID
KNOWN_TRACE_ID_NON_CANONICAL = "7EB23F43E1F44A67A64D1A481B36030F"


def assert_valid_trace_id(value: str) -> None:
    uuid.UUID(value)  # 非法格式直接抛 ValueError


@pytest.fixture
def api_client():
    """装载真实中间件与错误处理器的最小测试应用，覆盖业务错误与内部错误分支。"""
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    register_exception_handlers(app)

    class DemoBody(BaseModel):
        required_field: int

    @app.get("/demo")
    def demo() -> dict:
        return success_response({"ok": True}).model_dump(mode="json")

    @app.post("/demo/validate")
    def validate(_body: DemoBody) -> dict:
        return success_response({"ok": True}).model_dump(mode="json")

    @app.get("/demo/conflict")
    def conflict() -> dict:
        raise ApiError(20008, "请求与当前资源状态冲突", http_status=409)

    @app.get("/demo/boom")
    def boom() -> dict:
        raise RuntimeError("internal secret detail")

    # raise_server_exceptions=False：让 500 处理器像生产环境一样接管异常，
    # 否则 TestClient 默认会把服务器异常重新抛出。
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestSuccessEnvelope:
    def test_health_returns_success_envelope(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["msg"] == ""
        assert body["data"] == {"status": "ok"}
        assert_valid_trace_id(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]

    def test_success_response_uses_envelope(self, api_client):
        resp = api_client.get("/demo")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["msg"] == ""
        assert body["data"] == {"ok": True}
        assert_valid_trace_id(body["trace_id"])


class TestTraceId:
    def test_passthrough_valid_incoming_trace_id(self, client):
        resp = client.get("/health", headers={"X-Trace-Id": KNOWN_TRACE_ID})
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == KNOWN_TRACE_ID
        assert resp.headers["X-Trace-Id"] == KNOWN_TRACE_ID

    def test_generates_trace_id_when_missing(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert_valid_trace_id(resp.json()["trace_id"])
        assert_valid_trace_id(resp.headers["X-Trace-Id"])

    def test_ignores_invalid_incoming_trace_id(self, client):
        resp = client.get("/health", headers={"X-Trace-Id": "not-a-uuid"})
        assert resp.status_code == 200
        assert_valid_trace_id(resp.json()["trace_id"])

    def test_normalizes_non_canonical_incoming_trace_id(self, client):
        """契约要求 format: uuid 的规范 8-4-4-4-12 形式；非规范写法必须规范化。"""
        resp = client.get("/health", headers={"X-Trace-Id": KNOWN_TRACE_ID_NON_CANONICAL})
        assert resp.status_code == 200
        assert resp.json()["trace_id"] == KNOWN_TRACE_ID
        assert resp.headers["X-Trace-Id"] == KNOWN_TRACE_ID


class TestErrorEnvelope:
    def test_unknown_path_returns_20007_404(self, client):
        resp = client.get("/definitely-not-a-route")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 20007
        assert body["msg"] == "请求的资源不存在"
        assert_valid_trace_id(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]

    def test_business_api_error_returns_its_code(self, api_client):
        resp = api_client.get("/demo/conflict")
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == 20008
        assert body["msg"] == "请求与当前资源状态冲突"
        assert_valid_trace_id(body["trace_id"])

    def test_validation_error_returns_10003_400(self, api_client):
        resp = api_client.post("/demo/validate", json={"wrong_field": 1})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 10003
        assert body["msg"] == "请求参数不合法，请检查后重试"
        assert_valid_trace_id(body["trace_id"])

    def test_unhandled_error_returns_50000_500_without_details(self, api_client):
        resp = api_client.get("/demo/boom")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == 50000
        assert body["msg"] == "系统繁忙，请稍后再试"
        assert_valid_trace_id(body["trace_id"])
        # 500 由 ServerErrorMiddleware 经原始 send 发送，响应头必须显式回写 X-Trace-Id
        assert resp.headers["X-Trace-Id"] == body["trace_id"]
        # 不得泄露异常详情或内部信息
        assert "secret detail" not in resp.text

    def test_error_envelopes_keep_trace_id_in_response_header(self, api_client):
        resp = api_client.get("/demo/conflict")
        assert resp.headers["X-Trace-Id"] == resp.json()["trace_id"]
