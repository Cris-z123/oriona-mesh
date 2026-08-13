"""structlog JSON 结构化日志配置与敏感字段脱敏。

安全规则（决策 7）：日志必须过滤 ``password``、``token``、``secret_key`` 及其嵌套变体。
脱敏按键名递归匹配（不区分大小写），覆盖嵌套字典与列表；匹配键的取值统一替换为占位符，
绝不记录原值。模型调用审计（Phase 2 T033）在网关审计器中使用独立字段构造，不在本处理器
白名单之外泄露 payload；若审计字段名包含 ``token``（如 token 计数）须在 T033 明确命名边界。
"""

import logging
from typing import Any

import structlog
from structlog.types import EventDict

# 敏感键匹配子串（小写比较）；"secret" 同时覆盖 "secret_key" 及其变体。
_SENSITIVE_KEY_PATTERNS = ("password", "token", "secret")
_REDACTED = "[REDACTED]"


def _is_sensitive_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(pattern in lowered for pattern in _SENSITIVE_KEY_PATTERNS)


def redact_event_dict(value: Any) -> Any:
    """递归脱敏任意结构中的敏感键；返回新结构，不修改入参。"""
    return _redact(value, "")


def redact_sensitive(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog 处理器包装；按 ``(logger, method_name, event_dict)`` 契约调用。"""
    return redact_event_dict(event_dict)


def _redact(value: Any, key: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key) for item in value]
    if _is_sensitive_key(key):
        return _REDACTED
    return value


def add_trace_id(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    """把当前请求 trace_id 注入事件（来源：app.api.middleware.trace）。"""
    from app.api.middleware.trace import current_trace_id

    trace_id = current_trace_id()
    if trace_id:
        event_dict["trace_id"] = trace_id
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    """配置 structlog JSON 输出；调用一次后全局生效。"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_trace_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_sensitive,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
