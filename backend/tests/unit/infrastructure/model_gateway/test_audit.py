"""模型调用审计器单元测试（T036 / FR-029）。

验证审计事件只包含白名单元数据字段，且成功/失败事件都不含请求或响应正文、提示词、
用户问题、资料片段、文件名或凭证。
"""

from app.infrastructure.model_gateway.audit import (
    ALLOWED_AUDIT_FIELDS,
    ModelCallAudit,
    log_model_call,
)
from app.infrastructure.model_gateway.types import GatewayError


class TestAuditWhitelist:
    def test_allowed_fields_exact_set(self) -> None:
        assert set(ALLOWED_AUDIT_FIELDS) == {
            "trace_id",
            "call_id",
            "subject_digest",
            "call_type",
            "provider",
            "model",
            "started_at",
            "finished_at",
            "duration_ms",
            "status",
            "error_class",
            "retries",
            "input_tokens",
            "output_tokens",
            "payload_bytes",
        }

    def test_failed_audit_has_no_payload_fields(self) -> None:
        audit = ModelCallAudit(
            trace_id="t-1",
            call_id="c-1",
            subject_digest="digest",
            call_type="generation",
            provider="openai-compatible",
            model="gen-model",
            status="failed",
            started_at=100.0,
            finished_at=101.5,
            error_class=GatewayError("timeout", "x").error_class,
            retries=2,
            input_tokens=10,
            output_tokens=0,
            payload_bytes=128,
        )
        event = audit.to_whitelisted()
        assert event["duration_ms"] == 1500
        assert event["status"] == "failed"
        assert event["error_class"] == "timeout"
        for key in event:
            assert key in ALLOWED_AUDIT_FIELDS

    def test_audit_event_logs_without_payload(self, capsys) -> None:
        audit = ModelCallAudit(
            trace_id="t-2",
            call_id="c-2",
            subject_digest="digest",
            call_type="embedding",
            provider="openai-compatible",
            model="text-embedding-3-small",
            status="success",
            started_at=0.0,
            retries=0,
            payload_bytes=64,
        )
        log_model_call(audit)
        captured = capsys.readouterr()
        dumped = captured.err or captured.out
        assert "model_gateway_call" in dumped
        # 白名单字段出现；正文/提示词/文件名不得出现。
        assert '"call_type": "embedding"' in dumped or "embedding" in dumped
        for forbidden in ("prompt", "content", "filename", "authorization", "sk-test"):
            assert forbidden not in dumped
