"""LLM 用例适配器公共装配（T070/T071 / model-egress.md 调用信封）。

- 各适配器共用同一套调用上下文装配：不可逆用户摘要、trace_id 继承与最小调用
  上下文构造；避免在 embedding/rewrite/rerank/generation 四处重复并漂移；
- 摘要密钥始终来自注入的 :class:`Settings`（默认全局设置），保证测试注入生效。
"""

import uuid
from typing import Any

from app.api.middleware.trace import current_trace_id
from app.core.settings import Settings, get_settings
from app.infrastructure.model_gateway.types import CallType, ModelCall
from app.infrastructure.rate_limit.keys import user_fingerprint


def build_model_call(
    call_type: CallType,
    content: str,
    *,
    user_id: uuid.UUID,
    settings: Settings | None = None,
    trace_id: str | None = None,
    options: dict[str, Any] | None = None,
) -> ModelCall:
    """构造业务调用方提交的最小调用上下文（凭证与模型由网关决定）。

    ``options`` 只允许调用类型相关的非敏感参数（如 rerank 的候选数量
    ``candidate_count``），不得包含供应商凭证或请求头。
    """
    settings = settings or get_settings()
    secret = settings.rate_limit.subject_hmac_key.get_secret_value()
    return ModelCall(
        call_id=str(uuid.uuid4()),
        trace_id=trace_id or current_trace_id() or str(uuid.uuid4()),
        subject_digest=user_fingerprint(str(user_id), secret),
        call_type=call_type,
        content=content,
        options=options or {},
    )
