"""统一模型出口网关端口（model-egress.md）。

:class:`ModelGateway` 是四类外部模型调用的唯一出口：业务服务与 worker 只能通过它
调用外部模型。网关集中执行脱敏、路由、凭证注入、超时、重试、稳定失败分类与元数据
审计，并只返回最终成功或失败；网关不得执行领域降级。
"""

from collections.abc import Iterator
from typing import Protocol

from app.infrastructure.model_gateway.types import (
    GenerationDelta,
    ModelCall,
    ModelCallResult,
)


class ModelGateway(Protocol):
    """模型出口网关端口。"""

    def call(self, call: ModelCall) -> ModelCallResult:
        """执行一次模型调用（Embedding/Query Rewrite/Rerank/Generation）。

        成功返回对应结果；任何失败抛出 :class:`GatewayError`（含稳定失败分类）。
        脱敏失败或配置非法时不得发起任何外部请求。
        """
        ...

    def call_stream(self, call: ModelCall) -> Iterator[GenerationDelta]:
        """Generation 流式调用：产出文本增量，流结束携带 finish_reason。

        首 token 超时与总时长超时由网关执行；重试只发生在首 token 之前。
        """
        ...
