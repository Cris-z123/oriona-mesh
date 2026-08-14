"""v1 脱敏策略（model-egress.md 禁止外发与脱敏规则）。

- 始终删除：密码、访问/刷新令牌、API 密钥、Cookie、Authorization 与认证请求头、
  内部绝对存储路径、用户/租户原始标识（UUID）；
- 统一替换：邮箱、电话、身份证件号等个人标识使用本次调用内稳定且不可逆的占位符
  （每调用随机盐 + 摘要，跨调用不可关联）；
- 无法可靠处理时通过残留检测 fail-closed（抛出 :class:`SanitizationError`）。
"""

import hashlib
import re
import secrets
from typing import Any

_SENSITIVE_OPTION_KEYS = (
    "password",
    "token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "cookie",
    "headers",
)

_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_EMAIL_RE = re.compile(r"\b[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+\b")
_CN_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_CN_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 前导字符保留（\b 在空格与 / 之间不成立）；只替换路径本身。
_PATH_RE = re.compile(r"(?i)(^|[\s\"'(])(/data/orionamesh|/tmp/|/var/)[^\s\"']*")
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)[^/\s:@]+:[^@\s]+@")
# 移除认证头标签与值（"Authorization: Bearer abc.def.ghi" / 裸 "Bearer xxx"）。
_AUTH_HEADER_LABEL_RE = re.compile(r"(?i)\bauthorization\s*:\s*[^\s,;]+(?:\s+[^\s,;]+)*")
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
# 残留凭证值检测：脱敏后仍出现 key=value 形式的凭证赋值 → fail-closed。
_RESIDUAL_CREDENTIAL_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret|authorization|cookie|bearer)\s*[=:]\s*\S+"
)


class SanitizationError(Exception):
    """脱敏失败：禁止外发数据无法可靠处理，调用方必须 fail-closed。"""


class V1SanitizerPolicy:
    """版本 v1 的脱敏规则。"""

    version = "v1"

    def __init__(self) -> None:
        self._salt = secrets.token_hex(8)
        self._placeholders: dict[tuple[str, str], str] = {}

    def sanitize_text(self, text: str) -> str:
        if not text:
            return text
        result = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
        result = _AUTH_HEADER_LABEL_RE.sub("[REDACTED_AUTH]", result)
        result = _BEARER_TOKEN_RE.sub("[REDACTED_AUTH]", result)
        result = _PATH_RE.sub(r"\1[PATH]", result)
        result = _UUID_RE.sub("[ID]", result)
        result = _CN_PHONE_RE.sub(lambda m: self._placeholder("PHONE", m.group(0)), result)
        result = _CN_ID_RE.sub(lambda m: self._placeholder("ID_CARD", m.group(0)), result)
        result = _EMAIL_RE.sub(lambda m: self._placeholder("EMAIL", m.group(0)), result)
        if _RESIDUAL_CREDENTIAL_RE.search(result):
            raise SanitizationError("forbidden credential-like data cannot be sanitized")
        return result

    def sanitize_options(self, options: dict[str, Any]) -> dict[str, Any]:
        """删除敏感选项键（凭证、请求头等）；返回新字典。"""
        cleaned: dict[str, Any] = {}
        for key, value in options.items():
            lowered = str(key).lower()
            if any(pattern in lowered for pattern in _SENSITIVE_OPTION_KEYS):
                continue
            cleaned[key] = value
        return cleaned

    def _placeholder(self, label: str, original: str) -> str:
        key = (label, original)
        placeholder = self._placeholders.get(key)
        if placeholder is None:
            digest = hashlib.sha256((self._salt + original).encode("utf-8")).hexdigest()[:10]
            placeholder = f"[{label}:{digest}]"
            self._placeholders[key] = placeholder
        return placeholder


def build_policy(version: str) -> V1SanitizerPolicy:
    """按版本构建脱敏策略；未知版本必须失败（配置拒绝就绪）。"""
    if version != "v1":
        raise ValueError(f"unknown sanitizer policy version: {version!r}")
    return V1SanitizerPolicy()
