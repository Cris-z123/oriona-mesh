"""FastAPI 分级限流中间件（T029）。

- 端点策略与 OpenAPI ``x-rate-limit-policy`` 对齐；超限返回统一信封
  ``10005/429`` 与 ``Retry-After``，Redis 不可用时状态变更返回 ``50001/503``
  （fail-closed），只读 GET 按配置降级放行（fail-open）；
- 主体计算不可逆：账号用规范化邮箱 HMAC 摘要，刷新用 refresh token HMAC 指纹，
  已认证用户用 Bearer token 的 HMAC 指纹；来源 IP 只使用解析后的单个 IP；
  原始 token、邮箱与完整转发链绝不写入 Redis 或日志；
- 限流判断发生在业务写入之前：中间件在路由/依赖执行前拦截，超限请求不会产生
  任何资料、消息、任务或会话副作用；
- 实现为纯 ASGI 中间件，不缓冲流式响应（SSE）；仅在需要解析请求体主体的
  认证端点（注册/登录/刷新）读取小 JSON 请求体并原样回注 scope。
"""

import json
import re
from typing import Any

import redis as redis_lib
import structlog
from starlette.datastructures import Headers

from app.api.middleware.errors import ApiError
from app.api.middleware.trace import TRACE_HEADER, TRACE_ID_VAR
from app.api.v1.schemas.common import (
    PROTECTION_UNAVAILABLE_MSG,
    RATE_LIMIT_EXCEEDED_MSG,
    error_response,
)
from app.core.redis import get_redis_client
from app.core.security import decode_access_token, normalize_email
from app.core.settings import get_settings
from app.infrastructure.rate_limit.config import RateLimitSettings
from app.infrastructure.rate_limit.keys import (
    account_fingerprint,
    refresh_token_fingerprint,
    user_fingerprint,
)
from app.infrastructure.rate_limit.policies import (
    POLICY_AUTH_IP_AND_ACCOUNT,
    POLICY_AUTHENTICATED_DEFAULT,
    POLICY_QUESTION_USER,
    POLICY_REFRESH_IP_AND_TOKEN,
    POLICY_UPLOAD_USER,
    RateLimitPolicy,
    build_policies,
)
from app.infrastructure.rate_limit.redis_limiter import (
    RateLimitDecision,
    RateLimiter,
    RedisSlidingWindowLimiter,
)
from app.infrastructure.rate_limit.source_ip import resolve_source_ip

logger = structlog.get_logger()

_RATE_LIMIT_EXCEEDED_CODE = 10005
_PROTECTION_UNAVAILABLE_CODE = 50001

# 仅这些端点需要读取小 JSON 请求体（注册/登录/刷新）；上传等大主体端点不读体。
_BODY_READ_PATHS: dict[tuple[str, str], str] = {
    ("/v1/users", "POST"): "email",
    ("/v1/auth/sessions", "POST"): "email",
    ("/v1/auth/sessions", "PUT"): "refresh_token",
}
_MAX_BODY_BYTES = 64 * 1024

_AUTH_BEARER_RE = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)


class RateLimitMiddleware:
    """为 HTTP 请求执行端点限流；非 HTTP 请求直接透传。"""

    def __init__(
        self,
        app: Any,
        settings=None,
        limiter: RateLimiter | None = None,
        policies: dict[str, RateLimitPolicy] | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.app = app
        self.settings = settings
        self.rate_limit_settings: RateLimitSettings = settings.rate_limit
        self.policies = policies or build_policies(settings.rate_limit)
        self.limiter = limiter or RedisSlidingWindowLimiter(get_redis_client(settings))
        self.read_fail_open = settings.rate_limit.read_fail_open

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        policy_name = self._classify(scope)
        if policy_name is None:
            return await self.app(scope, receive, send)

        policy = self.policies[policy_name]
        subjects, receive = await self._resolve_subjects(scope, receive, policy_name)
        try:
            decision = self._check_policy(policy, subjects)
        except redis_lib.RedisError:
            if self._is_read_method(scope) and self.read_fail_open:
                # 只读 GET 按配置降级放行；日志只记录元数据，不含主体原值。
                logger.warning("rate_limit_unavailable_fail_open", policy=policy_name)
                return await self.app(scope, receive, send)
            logger.warning("rate_limit_unavailable_fail_closed", policy=policy_name)
            return await self._send_error(
                scope, send, 503, _PROTECTION_UNAVAILABLE_CODE, PROTECTION_UNAVAILABLE_MSG
            )

        if not decision.allowed:
            return await self._send_rate_limited(scope, send, decision)
        return await self.app(scope, receive, send)

    # ------------------------------------------------------------------
    # 策略分类
    # ------------------------------------------------------------------
    def _classify(self, scope: dict) -> str | None:
        path = scope["path"] or "/"
        method = (scope.get("method") or "").upper()
        if not path.startswith("/v1/"):
            return None

        if (path, method) in _BODY_READ_PATHS:
            return POLICY_AUTH_IP_AND_ACCOUNT if method != "PUT" else POLICY_REFRESH_IP_AND_TOKEN
        if method == "POST" and re.fullmatch(r"/v1/knowledge-bases/[^/]+/documents", path):
            return POLICY_UPLOAD_USER
        if method == "POST" and re.fullmatch(r"/v1/conversations/[^/]+/messages", path):
            return POLICY_QUESTION_USER
        return POLICY_AUTHENTICATED_DEFAULT

    @staticmethod
    def _is_read_method(scope: dict) -> bool:
        return (scope.get("method") or "").upper() in ("GET", "HEAD")

    # ------------------------------------------------------------------
    # 主体解析
    # ------------------------------------------------------------------
    async def _resolve_subjects(
        self, scope: dict, receive: Any, policy_name: str
    ) -> tuple[dict[str, str | None], Any]:
        """返回（主体摘要，下游 receive）。

        读取过请求体的路径必须把 buffered receive 传回：原始 receive 的 body 已被
        消费，下游再次调用会在 TestClient 等传输上等待请求完成而死锁。
        """
        headers = Headers(scope=scope)
        peer_ip = scope.get("client", ("", 0))[0] or ""

        subjects: dict[str, str | None] = {
            "ip": resolve_source_ip(
                peer_ip,
                headers.get("x-forwarded-for"),
                self.rate_limit_settings.trusted_proxy_networks,
            ),
            "account": None,
            "refresh_token": None,
            "user": None,
        }

        body_field = _BODY_READ_PATHS.get((scope["path"], (scope.get("method") or "").upper()))
        if body_field is not None:
            body = await self._read_body(receive)
            scope["body"] = body
            receive = _buffered_receive(body)
            parsed = self._parse_json_body(body)
            if body_field == "email":
                raw_email = parsed.get("email") if parsed else None
                if isinstance(raw_email, str):
                    try:
                        subjects["account"] = account_fingerprint(
                            normalize_email(raw_email),
                            self.rate_limit_settings.subject_hmac_key.get_secret_value(),
                        )
                    except ValueError:
                        subjects["account"] = None
            else:
                raw_token = parsed.get("refresh_token") if parsed else None
                if isinstance(raw_token, str) and raw_token:
                    subjects["refresh_token"] = refresh_token_fingerprint(
                        raw_token, self.rate_limit_settings.subject_hmac_key.get_secret_value()
                    )

        auth = headers.get("authorization")
        if auth:
            match = _AUTH_BEARER_RE.match(auth)
            if match:
                subjects["user"] = self._user_fingerprint(match.group(1))
        return subjects, receive

    def _user_fingerprint(self, access_token: str) -> str | None:
        """按 Access Token 解码出的用户 ``sub`` 生成不可逆限流键。

        FR-026 为“每用户”预算：同一用户重新登录（新 token）后预算不重置，
        因此先验证并解码 token 取其 sub，再做 HMAC；token 无效时返回 None
        （由下游认证依赖 401 拒绝，本规则跳过）。
        """
        try:
            sub = decode_access_token(access_token, get_settings().auth_jwt_secret_key_value)
        except ApiError:
            return None
        return user_fingerprint(sub, self.rate_limit_settings.subject_hmac_key.get_secret_value())

    async def _read_body(self, receive: Any) -> bytes:
        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > _MAX_BODY_BYTES:
                # 超出小 JSON 上限即放弃解析，主体回注由下游决定；不放大内存。
                break
            chunks.append(chunk)
            more = message.get("more_body", False)
        return b"".join(chunks)

    @staticmethod
    def _parse_json_body(body: bytes) -> dict[str, Any] | None:
        if not body:
            return None
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    # ------------------------------------------------------------------
    # 判定与响应
    # ------------------------------------------------------------------
    def _check_policy(
        self, policy: RateLimitPolicy, subjects: dict[str, str | None]
    ) -> RateLimitDecision:
        for rule in policy.rules:
            subject = subjects.get(rule.subject_kind)
            if subject is None:
                # 主体不可得（如登录体邮箱非法）：由下游校验拒绝，本规则跳过。
                continue
            key = self.limiter.build_key(policy.name, rule.window_seconds, subject)
            decision = self.limiter.check(key, rule.limit, rule.window_seconds)
            if not decision.allowed:
                return decision
        return RateLimitDecision(allowed=True)

    async def _send_rate_limited(self, scope: dict, send: Any, decision: RateLimitDecision) -> None:
        await self._send_error(
            scope,
            send,
            429,
            _RATE_LIMIT_EXCEEDED_CODE,
            RATE_LIMIT_EXCEEDED_MSG,
            extra_headers={"Retry-After": str(max(decision.retry_after_seconds, 1))},
        )

    async def _send_error(
        self,
        scope: dict,
        send: Any,
        status: int,
        code: int,
        msg: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        trace_id = TRACE_ID_VAR.get() or ""
        body = json.dumps(
            error_response(code, msg).model_dump(mode="json"), ensure_ascii=False
        ).encode("utf-8")
        headers = [(b"content-type", b"application/json; charset=utf-8")]
        if trace_id:
            headers.append((TRACE_HEADER.encode("ascii"), trace_id.encode("ascii")))
        if extra_headers:
            headers.extend((k.encode("ascii"), v.encode("ascii")) for k, v in extra_headers.items())
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})


def _buffered_receive(body: bytes) -> Any:
    """返回已消费 body 的 receive：下游读取到缓冲 body，后续调用返回空消息。

    限流中间件消费请求体后，原始 receive 的 body 已取走；若把原始 receive 传给
    下游，下游再次调用会在 TestClient 等传输上等待请求完成而死锁。
    """
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive
