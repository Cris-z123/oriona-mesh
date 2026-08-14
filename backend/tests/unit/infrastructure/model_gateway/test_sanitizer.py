"""模型出口脱敏器单元测试（T036 / FR-028）。

覆盖：禁止字段移除（凭证/路径/内部标识）、邮箱/电话/证件号不可逆占位符与调用内
稳定性、fail-closed 残留检测、选项键脱敏与未知策略版本拒绝。
"""

import uuid

import pytest

from app.infrastructure.model_gateway.policies.v1 import SanitizationError, build_policy
from app.infrastructure.model_gateway.sanitizer import sanitize_call
from app.infrastructure.model_gateway.types import ModelCall


def _call(content: str, options: dict | None = None) -> ModelCall:
    return ModelCall(
        call_id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        subject_digest="d" * 64,
        call_type="generation",
        content=content,
        options=options or {},
    )


class TestForbiddenFieldRemoval:
    def test_removes_email_phone_id_and_uuid(self) -> None:
        text = (
            "contact alice@example.com or 13800138000, id 11010119900307789X, "
            "user 3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        )
        result = sanitize_call(_call(text), "v1")
        assert "alice@example.com" not in result.content
        assert "13800138000" not in result.content
        assert "11010119900307789X" not in result.content
        assert "3f2504e0-4f89-41d3-9a0c-0305e82c3301" not in result.content
        assert "[EMAIL:" in result.content
        assert "[PHONE:" in result.content
        assert "[ID_CARD:" in result.content
        assert "[ID]" in result.content

    def test_removes_bearer_auth_and_url_credentials(self) -> None:
        text = "Authorization: Bearer abc.def.ghi and https://user:pass@example.com/x"
        result = sanitize_call(_call(text), "v1")
        assert "Bearer" not in result.content
        assert "abc.def.ghi" not in result.content
        assert "user:pass" not in result.content

    def test_removes_internal_storage_paths(self) -> None:
        text = "stored at /data/orionamesh/upload/abc/raw.pdf"
        result = sanitize_call(_call(text), "v1")
        assert "/data/orionamesh" not in result.content
        assert "[PATH]" in result.content

    def test_removes_sensitive_option_keys(self) -> None:
        result = sanitize_call(
            _call("question", {"api_key": "sk-123", "headers": {"x": "y"}, "keep": 1}),
            "v1",
        )
        assert "api_key" not in result.options
        assert "headers" not in result.options
        assert result.options == {"keep": 1}


class TestPlaceholderStability:
    def test_same_value_stable_within_call(self) -> None:
        text = "email a@b.co and again a@b.co"
        result = sanitize_call(_call(text), "v1")
        markers = [part for part in result.content.split() if part.startswith("[EMAIL:")]
        assert len(markers) == 2
        assert markers[0] == markers[1]

    def test_placeholders_differ_across_calls(self) -> None:
        r1 = sanitize_call(_call("mail a@b.co"), "v1")
        r2 = sanitize_call(_call("mail a@b.co"), "v1")
        assert r1.content != r2.content


class TestFailClosed:
    def test_residual_credential_value_fails_closed(self) -> None:
        with pytest.raises(SanitizationError):
            sanitize_call(_call("leak token=abc123def"), "v1")
        with pytest.raises(SanitizationError):
            sanitize_call(_call("api_key: sk-secret-value-here"), "v1")

    def test_unknown_policy_version_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_policy("v999")
        with pytest.raises(SanitizationError):
            sanitize_call(_call("text"), "v999")


class TestPolicyV1:
    def test_policy_version_is_v1(self) -> None:
        assert build_policy("v1").version == "v1"

    def test_plain_text_passes_through(self) -> None:
        result = sanitize_call(_call("what is the capital of france?"), "v1")
        assert result.content == "what is the capital of france?"
