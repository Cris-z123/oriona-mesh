"""结构化日志脱敏单元测试（T009）。

安全规则（决策 7 / quickstart）：日志必须过滤 ``password``、``token``、``secret_key``
及其嵌套变体；允许字段（如 email、文件名、调用元数据）不得被误伤。
"""

import json
from io import StringIO

import pytest
import structlog

from app.core.logging import configure_logging, redact_event_dict, redact_sensitive

pytestmark = pytest.mark.unit


class TestRedactSensitive:
    def test_redacts_top_level_sensitive_keys(self):
        event = {"password": "p", "access_token": "t", "secret_key": "s", "email": "a@b.c"}
        result = redact_event_dict(event)
        assert result["password"] == "[REDACTED]"
        assert result["access_token"] == "[REDACTED]"
        assert result["secret_key"] == "[REDACTED]"
        assert result["email"] == "a@b.c"

    def test_redacts_case_insensitive_and_camel_case_keys(self):
        event = {"PASSWORD": "p", "RefreshToken": "t", "apiSecretKey": "s"}
        result = redact_event_dict(event)
        assert result == {
            "PASSWORD": "[REDACTED]",
            "RefreshToken": "[REDACTED]",
            "apiSecretKey": "[REDACTED]",
        }

    def test_redacts_nested_dicts(self):
        event = {"user": {"credentials": {"password": "p", "email": "a@b.c"}}}
        result = redact_event_dict(event)
        assert result["user"]["credentials"]["password"] == "[REDACTED]"
        assert result["user"]["credentials"]["email"] == "a@b.c"

    def test_redacts_items_in_lists(self):
        event = {"items": [{"token": "t", "id": 1}, {"secret_key": "s", "id": 2}]}
        result = redact_event_dict(event)
        assert result["items"][0]["token"] == "[REDACTED]"
        assert result["items"][0]["id"] == 1
        assert result["items"][1]["secret_key"] == "[REDACTED]"

    def test_non_sensitive_fields_untouched(self):
        event = {
            "trace_id": "7eb23f43-e1f4-4a67-a64d-1a481b36030f",
            "code": 0,
            "duration_ms": 12,
            "nested": {"msg": "ok"},
        }
        assert redact_event_dict(event) == event

    def test_does_not_mutate_input(self):
        event = {"password": "p", "ok": {"v": 1}}
        redact_event_dict(event)
        assert event["password"] == "p"

    def test_scalar_values_pass_through(self):
        assert redact_event_dict("plain") == "plain"
        assert redact_event_dict(42) == 42
        assert redact_event_dict(None) is None


class TestConfiguredLogging:
    def test_json_output_redacts_sensitive_values(self):
        out = StringIO()
        configure_logging()
        # 使用 StringIO 捕获 JSON 行
        logger = structlog.wrap_logger(
            structlog.PrintLogger(file=out),
            processors=[
                redact_sensitive,
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(20),
        )
        logger.info("auth_attempt", password="hunter2", email="a@b.c")
        record = json.loads(out.getvalue())
        assert record["password"] == "[REDACTED]"
        assert record["email"] == "a@b.c"
        assert "[REDACTED]" not in record["email"]

    def test_configured_chain_emits_redacted_json(self, capsys):
        configure_logging()
        structlog.get_logger("test.chain").info(
            "login",
            password="hunter2",
            refresh_token="rt_secret",
            email="a@b.c",
        )
        out = capsys.readouterr().out
        lines = [line for line in out.strip().splitlines() if line]
        assert lines, "expected at least one JSON log line"
        record = json.loads(lines[-1])
        assert record["event"] == "login"
        assert record["password"] == "[REDACTED]"
        assert record["refresh_token"] == "[REDACTED]"
        assert record["email"] == "a@b.c"
        assert "hunter2" not in out
