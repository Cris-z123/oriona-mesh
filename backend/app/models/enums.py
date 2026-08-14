"""领域枚举定义。

所有状态枚举独立声明（不共享字符串表），数据库层以原生 PostgreSQL 枚举保存；
公开 DTO 的状态子集与过滤规则由 API 层映射（data-model.md / openapi.yaml）。
"""

from enum import StrEnum
from typing import Any


def enum_values(enum_cls: type[StrEnum]) -> list[Any]:
    """SQLAlchemy ``values_callable``：以成员值（小写）而非成员名建原生枚举。

    与迁移 0002 的 ``CREATE TYPE ... AS ENUM ('active', ...)`` 保持一致，
    否则 create_all 会生成大写成员名类型并与服务端默认值冲突。
    """
    return [member.value for member in enum_cls]


class KnowledgeBaseStatus(StrEnum):
    """知识库生命周期状态（data-model.md：active/deleting/delete_failed）。"""

    ACTIVE = "active"
    DELETING = "deleting"
    DELETE_FAILED = "delete_failed"


class DocumentStatus(StrEnum):
    """资料处理状态。

    公开状态（pending/queued/processing/completed/failed）与内部隐藏状态
    （deleting/deleted）共用一个数据库枚举；API 层保证隐藏状态不进入公开 DTO。
    """

    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class DocumentTaskType(StrEnum):
    """资料处理阶段任务类型。

    cleanup 仅清理旧版本；delete_cleanup 仅用于已删除资料的清理，二者不得混用。
    """

    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    FINALIZE = "finalize"
    CLEANUP = "cleanup"
    DELETE_CLEANUP = "delete_cleanup"


class DocumentTaskStatus(StrEnum):
    """后台任务生命周期状态。"""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DocumentAttemptStatus(StrEnum):
    """单次任务尝试状态；同一任务最多一个未结束（running）尝试。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileType(StrEnum):
    """支持上传的文件类型。"""

    PDF = "pdf"
    DOCX = "docx"
    MD = "md"
    TXT = "txt"


class UploadRequestStatus(StrEnum):
    """批量上传幂等记录状态。"""

    COORDINATING = "coordinating"
    ACCEPTED = "accepted"
    FAILED = "failed"


class MessageRole(StrEnum):
    """对话消息角色。"""

    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    """消息状态。

    user 消息固定为 completed；assistant 消息为 streaming 或明确终态
    （completed/failed/cancelled），任何分支都不得遗留永久 streaming。
    """

    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageFinishReason(StrEnum):
    """助手消息结束原因，与终态严格配对（openapi.yaml AssistantMessage）。"""

    STOP = "stop"
    LENGTH = "length"
    ERROR = "error"
    CANCELLED = "cancelled"
