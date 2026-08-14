"""知识库请求/响应模式（openapi.yaml knowledge-bases 段）。"""

from pydantic import BaseModel, Field, model_validator

from app.models.constants import DELETE_CLEANUP_ERROR_CODE
from app.models.enums import KnowledgeBaseStatus


class KnowledgeBaseInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class UpdateKnowledgeBaseInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UpdateKnowledgeBaseInput":
        # OpenAPI minProperties: 1：空 PATCH 必须 10003/400。
        if self.name is None and self.description is None:
            raise ValueError("at least one field must be provided")
        return self


def knowledge_base_dto(kb) -> dict:
    """KnowledgeBase 响应模式。

    active 返回完整对象与 ``delete``；delete_failed 仅返回最小“删除未完成”墓碑与
    ``retry_delete``（名称/描述置空，不暴露子资源入口）。
    """
    if kb.status == KnowledgeBaseStatus.DELETE_FAILED:
        return {
            "id": str(kb.id),
            "name": None,
            "description": None,
            "status": KnowledgeBaseStatus.DELETE_FAILED.value,
            "delete_error_code": DELETE_CLEANUP_ERROR_CODE,
            "allowed_actions": ["retry_delete"],
            "created_at": kb.created_at.isoformat(),
            "updated_at": kb.updated_at.isoformat(),
        }
    return {
        "id": str(kb.id),
        "name": kb.name,
        "description": kb.description,
        "status": KnowledgeBaseStatus.ACTIVE.value,
        "delete_error_code": None,
        "allowed_actions": ["delete"],
        "created_at": kb.created_at.isoformat(),
        "updated_at": kb.updated_at.isoformat(),
    }
