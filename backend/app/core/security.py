"""认证安全核心：邮箱规范化、密码哈希、Access/Refresh Token。

安全规则（FR-001 / quickstart 认证配置契约）：
- 邮箱唯一规范化函数：先去除首尾 Unicode 空白并完成格式校验，再对完整值执行 Unicode
  ``casefold``；结果同时用于 users.email 存储、注册冲突、登录查找和账号限流 HMAC；
- Access Token 固定 HS256、2 小时 TTL（代码常量，无环境变量覆盖）；验证端只接受
  ``sub/iat/exp/type=access`` 且不得按 token 头动态选择算法；任何验证失败统一
  ``10001/401``；
- Refresh Token 为 ``rt_`` + 32 字节 CSPRNG 的无填充 Base64URL（总长 46），不是 JWT；
  服务端只保存 SHA-256 摘要；
- 密码使用 PBKDF2-HMAC-SHA256（stdlib，无原生依赖），盐值随机并随哈希存储。
"""

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from email_validator import validate_email as _validate_email

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import TOKEN_INVALID_MSG

# --- 常量（安全代码常量，不提供环境变量覆盖） ---
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL_SECONDS = 7200
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_PREFIX = "rt_"
REFRESH_TOKEN_RAW_BYTES = 32
REFRESH_TOKEN_LIFETIME_DAYS = 7
# 密码哈希参数
_PBKDF2_ALGORITHM = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16
_DERIVED_BYTES = 32

# 统一 10001 提示与状态（FR-001）
_INVALID_TOKEN_MSG = TOKEN_INVALID_MSG
_INVALID_TOKEN_STATUS = 401
_INVALID_TOKEN_CODE = 10001


# --------------------------------------------------------------------------
# 邮箱规范化
# --------------------------------------------------------------------------
def normalize_email(value: str) -> str:
    """邮箱唯一规范化函数。

    去除首尾 Unicode 空白 → 格式校验（RFC 5321）→ 对完整值 Unicode casefold。
    格式非法或去除空白后为空时抛出 :class:`ValueError`，由调用方映射为业务错误。
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("email must not be blank")
    # check_deliverability=False：不发起 DNS/网络查询，仅做格式校验。
    _validate_email(stripped, check_deliverability=False)
    return stripped.casefold()


# --------------------------------------------------------------------------
# 密码哈希
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """生成 PBKDF2-HMAC-SHA256 密码哈希，格式 ``pbkdf2_sha256$iterations$salt$hash``。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "$".join(
        (
            _PBKDF2_ALGORITHM,
            str(_PBKDF2_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(derived).decode("ascii").rstrip("="),
        )
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """校验密码；存储格式非法时返回 False，不抛出细节。"""
    try:
        algorithm, iterations_str, salt_b64, hash_b64 = stored_hash.split("$")
        iterations = int(iterations_str)
        salt = _b64decode(salt_b64)
        expected = _b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    if algorithm != _PBKDF2_ALGORITHM or iterations < 1 or not salt or not expected:
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# --------------------------------------------------------------------------
# Access Token（JWT）
# --------------------------------------------------------------------------
def create_access_token(user_id: str, secret_key: str) -> str:
    """签发固定 HS256 的 2 小时 Access Token。"""
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
        "type": ACCESS_TOKEN_TYPE,
    }
    return jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret_key: str) -> str:
    """解码并验证 Access Token，返回 ``sub``。

    缺失声明、格式错误、签名或算法无效、type 错误、过期统一抛出
    ``10001/401``（FR-001）。只接受 HS256，绝不按 token 头动态选择算法。
    """
    try:
        claims = jwt.decode(
            token,
            secret_key,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp", "type"]},
        )
    except jwt.PyJWTError:
        raise _invalid_token() from None
    if claims.get("type") != ACCESS_TOKEN_TYPE:
        raise _invalid_token()
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _invalid_token()
    return sub


def _invalid_token() -> ApiError:
    return ApiError(_INVALID_TOKEN_CODE, _INVALID_TOKEN_MSG, _INVALID_TOKEN_STATUS)


# --------------------------------------------------------------------------
# Refresh Token（不透明随机令牌）
# --------------------------------------------------------------------------
def generate_refresh_token() -> str:
    """生成 ``rt_`` + 32 字节 CSPRNG 的无填充 Base64URL 编码（总长 46）。"""
    raw = secrets.token_bytes(REFRESH_TOKEN_RAW_BYTES)
    return REFRESH_TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def refresh_token_hash(refresh_token: str) -> str:
    """返回 refresh token 的 SHA-256 十六进制摘要；服务端只保存该摘要。

    使用 UTF-8 编码作为纵深防御：schema 层已按 ``rt_`` + Base64URL 模式拒绝
    非 ASCII 输入，这里即使收到异常输入也不抛 ``UnicodeEncodeError``（统一由
    下游按无效 token 处理）。
    """
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
