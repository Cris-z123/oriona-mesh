"""检索与消息恢复配置（quickstart 检索与消息恢复配置契约）。

- ``RETRIEVAL_VECTOR_MIN_SIMILARITY`` / ``RETRIEVAL_TRGM_MIN_SIMILARITY``：两路候选
  证据门槛，必须在闭区间 [0, 1]；低于门槛的候选在 RRF 前排除；
- ``MESSAGE_STREAMING_STALE_SECONDS``：assistant streaming 最大存续时间；必须不小于
  全部 Query Rewrite/Reranker/Generation 最大尝试预算之和加 60 秒。
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MIN_STALE_SECONDS = 60


class RetrievalSettings(BaseSettings):
    """检索证据门槛与消息 streaming 失联上限。"""

    model_config = SettingsConfigDict(env_prefix="RETRIEVAL_", extra="ignore")

    vector_min_similarity: float = Field(default=0.65, ge=0.0, le=1.0)
    trgm_min_similarity: float = Field(default=0.30, ge=0.0, le=1.0)
    # 前缀归属消息恢复域；显式别名保持 quickstart 变量名。
    message_streaming_stale_seconds: int = Field(
        default=360,
        ge=_MIN_STALE_SECONDS,
        validation_alias="MESSAGE_STREAMING_STALE_SECONDS",
    )

    @model_validator(mode="after")
    def _validate(self) -> "RetrievalSettings":
        if not (0.0 <= self.vector_min_similarity <= 1.0):
            raise ValueError("RETRIEVAL_VECTOR_MIN_SIMILARITY must be in [0, 1]")
        if not (0.0 <= self.trgm_min_similarity <= 1.0):
            raise ValueError("RETRIEVAL_TRGM_MIN_SIMILARITY must be in [0, 1]")
        return self

    def validate_streaming_stale(self, attempt_budget_sum: int) -> None:
        """校验 streaming 失联上限覆盖全部模型尝试预算加 60 秒；不满足则拒绝就绪。"""
        required = attempt_budget_sum + _MIN_STALE_SECONDS
        if self.message_streaming_stale_seconds < required:
            raise ValueError(
                "MESSAGE_STREAMING_STALE_SECONDS must be at least "
                f"{required} (attempt budget {attempt_budget_sum} + 60s), "
                f"got {self.message_streaming_stale_seconds}"
            )
