"""限流主体摘要与 Redis 键（FR-026 / data-model 基础设施数据边界）。

- 账号（规范化邮箱）、已认证用户标识与 refresh token 指纹必须使用带服务端秘密的
  不可逆 HMAC 摘要；禁止在 Redis 键或成员中保存邮箱、用户 UUID 或原始令牌；
- 来源 IP 本身非秘密，键中允许使用解析后的单个 IP；完整转发链禁止入库。
"""

import hashlib
import hmac as hmac_lib

_PREFIX = "rl"


def subject_hmac(value: str, secret_key: str) -> str:
    """带服务端秘密的不可逆主体摘要（HMAC-SHA256 十六进制）。"""
    return hmac_lib.new(
        secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def account_fingerprint(normalized_email: str, secret_key: str) -> str:
    """规范化邮箱账号指纹（注册/登录限流）。"""
    return subject_hmac(normalized_email, secret_key)


def refresh_token_fingerprint(refresh_token: str, secret_key: str) -> str:
    """refresh token HMAC 指纹（刷新限流）；先 SHA-256 再 HMAC，杜绝令牌结构特征。

    UTF-8 编码容忍非 ASCII 输入（schema 层已拒绝，此处为纵深防御）。
    """
    digest = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    return subject_hmac(digest, secret_key)


def user_fingerprint(user_id: str, secret_key: str) -> str:
    """已认证用户标识指纹（上传/问答/默认策略限流）。"""
    return subject_hmac(user_id, secret_key)


def rate_limit_key(policy_name: str, window_seconds: int, subject: str) -> str:
    """Redis 限流键；subject 必须是不可逆摘要或解析后的单个来源 IP。"""
    return f"{_PREFIX}:{policy_name}:{window_seconds}:{subject}"
