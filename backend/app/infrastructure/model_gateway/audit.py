"""模型调用元数据审计器（FR-029 / model-egress.md 日志元数据白名单）。

审计事件只允许以下字段：``trace_id``、调用 ID、不可逆主体摘要、调用类型、供应商、
模型、开始/结束时间、耗时、状态、错误分类、重试次数、输入/输出 token 数量与载荷
字节数。禁止请求/响应正文、提示词、用户问题、资料片段、文件名、请求头、凭证及其
可逆摘要，也不得记录脱敏后的正文。
"""

import time
from dataclasses import dataclass, field
from typing import Literal

import structlog

from app.infrastructure.model_gateway.types import CallType, GatewayErrorClass

logger = structlog.get_logger()

AuditStatus = Literal["success", "failed"]

# 严格白名单：事件只允许这些键。
ALLOWED_AUDIT_FIELDS: tuple[str, ...] = (
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
)


@dataclass(frozen=True)
class ModelCallAudit:
    """一次模型调用的白名单审计元数据。"""

    trace_id: str
    call_id: str
    subject_digest: str
    call_type: CallType
    provider: str
    model: str
    status: AuditStatus
    started_at: float
    finished_at: float = field(default_factory=time.time)
    error_class: GatewayErrorClass | None = None
    retries: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    payload_bytes: int = 0

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at) * 1000))

    def to_whitelisted(self) -> dict:
        """只输出白名单字段；任何非白名单键都会被丢弃。"""
        return {key: getattr(self, key) for key in ALLOWED_AUDIT_FIELDS if hasattr(self, key)}


def log_model_call(audit: ModelCallAudit) -> None:
    """记录一次模型调用的白名单审计事件（成功/失败/重试降级共用）。"""
    logger.info("model_gateway_call", **audit.to_whitelisted())
