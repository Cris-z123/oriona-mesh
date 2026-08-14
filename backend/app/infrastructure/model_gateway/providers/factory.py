"""配置驱动的供应商适配器工厂（T034）。

未知 provider 必须在启动就绪阶段失败（不得静默回退或绕过网关）；凭证只在此处从
配置注入适配器（发送边界）。
"""

from app.infrastructure.model_gateway.config import ModelGatewaySettings
from app.infrastructure.model_gateway.providers.base import ProviderAdapter
from app.infrastructure.model_gateway.providers.openai_compatible import OpenAICompatibleAdapter


def build_provider_adapter(settings: ModelGatewaySettings) -> ProviderAdapter:
    """按配置构建供应商适配器；provider 未知或 endpoint 缺失时抛错。"""
    if settings.provider != "openai-compatible":
        raise ValueError(f"unknown model gateway provider: {settings.provider!r}")
    if not settings.endpoint:
        raise ValueError("MODEL_GATEWAY_ENDPOINT is required")
    return OpenAICompatibleAdapter(
        endpoint=settings.endpoint,
        api_key=settings.api_key.get_secret_value(),
    )
