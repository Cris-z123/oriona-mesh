"""安全核心单元测试（T018）。

覆盖邮箱规范化的单一算法复用、密码哈希、HS256 白名单与必填声明、2 小时 Access JWT
时长、全部 Access Token 验证失败统一 10001/401，以及 Refresh Token 的格式、随机性、
非 JWT 与仅 SHA-256 摘要落库；敏感令牌不进入日志（复用脱敏处理器验证）。
"""

import base64
import re
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.api.middleware.errors import ApiError
from app.core.logging import redact_event_dict
from app.core.security import (
    ACCESS_TOKEN_TTL_SECONDS,
    JWT_ALGORITHM,
    REFRESH_TOKEN_PREFIX,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    normalize_email,
    refresh_token_hash,
    verify_password,
)

# 仅用于测试的密钥；长度满足快速验证要求。
SECRET = "x" * 64


# --------------------------------------------------------------------------
# 邮箱规范化（存储/登录/限流复用同一函数）
# --------------------------------------------------------------------------
class TestNormalizeEmail:
    def test_strips_unicode_whitespace_and_casefolds(self) -> None:
        assert normalize_email("  USER@Example.COM 　") == "user@example.com"
        assert normalize_email("\t MiXeD@DoMaIn.IO ") == "mixed@domain.io"

    def test_casefold_applies_to_full_value(self) -> None:
        # 完整值 casefold（含 local part 与 domain；ß → ss）；同一输入恒等输出。
        result = normalize_email("Straße@Example.COM")
        assert result == "strasse@example.com"
        assert normalize_email(result) == result

    def test_invalid_format_raises_value_error(self) -> None:
        for bad in ("not-an-email", "a@b", "user@", "@domain.com", "a b@c.com"):
            with pytest.raises(ValueError):
                normalize_email(bad)

    def test_blank_after_strip_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_email("   　  ")


# --------------------------------------------------------------------------
# 密码哈希
# --------------------------------------------------------------------------
class TestPasswordHash:
    def test_hash_and_verify_roundtrip(self) -> None:
        stored = hash_password("correct horse battery")
        assert verify_password("correct horse battery", stored)
        assert not verify_password("wrong password", stored)

    def test_stored_format_contains_algorithm_and_iterations(self) -> None:
        stored = hash_password("s3cret")
        parts = stored.split("$")
        assert parts[0] == "pbkdf2_sha256"
        assert int(parts[1]) >= 100_000

    def test_salt_randomness_two_hashes_differ(self) -> None:
        assert hash_password("same") != hash_password("same")

    def test_verify_rejects_malformed_stored_hash(self) -> None:
        for malformed in ("", "not-a-hash", "pbkdf2_sha256$0$ab$cd", "pbkdf2_sha256$x$ab$cd"):
            assert verify_password("anything", malformed) is False


# --------------------------------------------------------------------------
# Access Token（HS256 白名单 / 必填声明 / 2 小时 TTL / 统一 10001/401）
# --------------------------------------------------------------------------
class TestAccessToken:
    def test_create_decode_roundtrip(self) -> None:
        token = create_access_token("11111111-1111-4111-8111-111111111111", SECRET)
        assert decode_access_token(token, SECRET) == "11111111-1111-4111-8111-111111111111"

    def test_claims_and_algorithm(self) -> None:
        token = create_access_token("user-1", SECRET)
        header = jwt.get_unverified_header(token)
        assert header["alg"] == JWT_ALGORITHM == "HS256"
        claims = jwt.decode(token, SECRET, algorithms=[JWT_ALGORITHM])
        assert set(("sub", "iat", "exp", "type")) <= set(claims)
        assert claims["type"] == "access"

    def test_ttl_is_fixed_two_hours(self) -> None:
        token = create_access_token("user-1", SECRET)
        claims = jwt.decode(token, SECRET, algorithms=[JWT_ALGORITHM])
        assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_TTL_SECONDS == 7200

    def test_fixed_ttl_not_influenced_by_clock_skew_inputs(self) -> None:
        # 时长是代码常量，不由调用方控制。
        assert ACCESS_TOKEN_TTL_SECONDS == 7200

    @pytest.mark.parametrize(
        ("bad_token", "description"),
        [
            ("", "missing token"),
            ("not.a.jwt", "malformed"),
            ("abc", "too short"),
        ],
    )
    def test_missing_or_malformed_rejected(self, bad_token: str, description: str) -> None:
        with pytest.raises(ApiError) as exc:
            decode_access_token(bad_token, SECRET)
        assert exc.value.code == 10001
        assert exc.value.http_status == 401

    def test_wrong_signature_rejected(self) -> None:
        token = create_access_token("user-1", SECRET)
        with pytest.raises(ApiError):
            decode_access_token(token, "y" * 64)

    def test_wrong_algorithm_header_rejected(self) -> None:
        # 即使 header 声称 HS512，验证端也只接受 HS256（不按 header 动态选择算法）。
        token = jwt.encode(
            {
                "sub": "u",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
                "type": "access",
            },
            SECRET,
            algorithm="HS512",
        )
        with pytest.raises(ApiError):
            decode_access_token(token, SECRET)

    def test_missing_claims_rejected(self) -> None:
        now = datetime.now(UTC)
        cases = [
            {"sub": "u", "exp": now + timedelta(hours=1), "type": "access"},  # 缺 iat
            {"iat": now, "exp": now + timedelta(hours=1), "type": "access"},  # 缺 sub
            {"sub": "u", "iat": now, "type": "access"},  # 缺 exp
            {"sub": "u", "iat": now, "exp": now + timedelta(hours=1)},  # 缺 type
        ]
        for payload in cases:
            token = jwt.encode(payload, SECRET, algorithm=JWT_ALGORITHM)
            with pytest.raises(ApiError) as exc:
                decode_access_token(token, SECRET)
            assert exc.value.code == 10001

    def test_wrong_type_rejected(self) -> None:
        now = datetime.now(UTC)
        token = jwt.encode(
            {"sub": "u", "iat": now, "exp": now + timedelta(hours=1), "type": "refresh"},
            SECRET,
            algorithm=JWT_ALGORITHM,
        )
        with pytest.raises(ApiError) as exc:
            decode_access_token(token, SECRET)
        assert exc.value.code == 10001

    def test_expired_rejected(self) -> None:
        past = datetime.now(UTC) - timedelta(hours=1)
        token = jwt.encode(
            {"sub": "u", "iat": past - timedelta(hours=1), "exp": past, "type": "access"},
            SECRET,
            algorithm=JWT_ALGORITHM,
        )
        with pytest.raises(ApiError) as exc:
            decode_access_token(token, SECRET)
        assert exc.value.code == 10001
        assert exc.value.http_status == 401


# --------------------------------------------------------------------------
# Refresh Token（格式 / 随机性 / 非 JWT / 仅摘要落库）
# --------------------------------------------------------------------------
class TestRefreshToken:
    def test_format_prefix_length_and_charset(self) -> None:
        token = generate_refresh_token()
        assert token.startswith(REFRESH_TOKEN_PREFIX)
        assert len(token) == 46
        # rt_ + 43 个无填充 Base64URL 字符
        assert re.fullmatch(r"rt_[A-Za-z0-9_-]{43}", token) is not None

    def test_randomness(self) -> None:
        assert generate_refresh_token() != generate_refresh_token()

    def test_not_a_jwt(self) -> None:
        token = generate_refresh_token()
        # 不是 JWT：无三个点分隔段，且解码必然失败。
        assert token.count(".") == 0
        with pytest.raises(jwt.exceptions.DecodeError):
            jwt.decode(token, SECRET, algorithms=[JWT_ALGORITHM])

    def test_hash_is_sha256_hex_only(self) -> None:
        token = generate_refresh_token()
        digest = refresh_token_hash(token)
        assert len(digest) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        assert token not in digest
        # 摘要确定性；不同 token 摘要不同。
        assert refresh_token_hash(token) == digest
        assert refresh_token_hash(generate_refresh_token()) != digest

    def test_decoded_refresh_bytes_are_32(self) -> None:
        token = generate_refresh_token()
        raw = base64.urlsafe_b64decode(token[len(REFRESH_TOKEN_PREFIX) :] + "=")
        assert len(raw) == 32


# --------------------------------------------------------------------------
# 敏感令牌不落日志
# --------------------------------------------------------------------------
class TestTokenLogRedaction:
    def test_access_and_refresh_tokens_redacted_from_logs(self) -> None:
        access = create_access_token("user-1", SECRET)
        refresh = generate_refresh_token()
        event = {
            "access_token": access,
            "refresh_token": refresh,
            "nested": {"token": refresh, "keep": "ok"},
        }
        redacted = redact_event_dict(event)
        dumped = str(redacted)
        assert access not in dumped
        assert refresh not in dumped
        assert redacted["access_token"] == "[REDACTED]"
        assert redacted["nested"]["token"] == "[REDACTED]"
        assert redacted["nested"]["keep"] == "ok"
