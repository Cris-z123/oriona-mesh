"""资料上传/详情/列表/任务响应模式（T049 / openapi.yaml documents 段）。

- 公开 Document 状态与过滤枚举仅允许 pending/queued/processing/completed/failed；
  内部 deleting/deleted 传入时由 Pydantic 校验拒绝（``10003/400``）；
- Document/DocumentTask DTO 的可空 ``error_code`` 限定为
  ``20001/20010~20015/50000``，并映射固定安全提示；
- ``202`` 上传项的 DTO 只允许 queued 或 failed/20011（openapi
  ``DocumentUploadItem`` oneOf）。
"""

from enum import StrEnum

from app.api.v1.schemas.common import DEFAULT_ERROR_MSG
from app.models.constants import DELETE_CLEANUP_ERROR_CODE
from app.models.enums import (
    DocumentStatus,
    DocumentTaskType,
    FileType,
)

# 异步失败稳定安全提示（openapi.yaml Document.error_message）。
ASYNC_ERROR_MESSAGES: dict[int, str] = {
    20001: "资料解析失败，请删除后重新上传",
    20010: "资料内容为空，请删除后重新上传",
    20011: "文件保存失败，请删除后重新上传",
    20012: "资料向量化失败，请删除后重新上传",
    20013: "资料处理结果不一致，请删除后重新上传",
    20014: "资料处理失败，请删除后重新上传",
    20015: "资料删除未完成，请重试删除",
    50000: DEFAULT_ERROR_MSG,
}


class PublicDocumentStatus(StrEnum):
    """公开资料状态过滤枚举；内部状态不在此列。"""

    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


def _file_type_value(file_type: FileType) -> str:
    return file_type.value


def document_dto(doc) -> dict:
    """Document 响应模式；allowed_actions 由服务端按当前状态计算。"""
    if _is_delete_cleanup_failed(doc):
        allowed_actions = ["retry_delete"]
    else:
        allowed_actions = ["delete"]
    return {
        "id": str(doc.id),
        "knowledge_base_id": str(doc.knowledge_base_id),
        "filename": doc.filename,
        "file_type": _file_type_value(doc.file_type),
        "file_size": doc.file_size,
        "status": doc.status.value,
        "version": doc.version,
        "current_task_type": doc.current_task_type.value if doc.current_task_type else None,
        "retry_count": doc.retry_count,
        "delete_cycle": doc.delete_cycle,
        "chunk_count": doc.chunk_count,
        "error_code": doc.error_code,
        "error_message": _error_message(doc),
        "processing_started_at": _iso(doc.processing_started_at),
        "processing_finished_at": _iso(doc.processing_finished_at),
        "created_at": _iso(doc.created_at),
        "updated_at": _iso(doc.updated_at),
        "allowed_actions": allowed_actions,
    }


def document_upload_item_dto(doc) -> dict:
    """202 上传收敛项；只允许 queued 或 failed/20011（openapi oneOf）。"""
    dto = document_dto(doc)
    if dto["status"] == DocumentStatus.QUEUED.value:
        dto["current_task_type"] = DocumentTaskType.PARSE.value
        dto["error_code"] = None
        dto["error_message"] = None
    elif dto["status"] == DocumentStatus.FAILED.value and dto["error_code"] == 20011:
        dto["current_task_type"] = DocumentTaskType.PARSE.value
        dto["error_message"] = ASYNC_ERROR_MESSAGES[20011]
    else:
        raise ValueError(f"invalid 202 upload item state: {dto['status']}/{dto['error_code']}")
    return dto


def document_task_attempt_dto(attempt) -> dict:
    """DocumentTaskAttempt 响应模式（worker、非空 started_at、可空终态字段）。"""
    return {
        "id": str(attempt.id),
        "task_id": str(attempt.task_id),
        "attempt_no": attempt.attempt_no,
        "worker_name": attempt.worker_name,
        "status": attempt.status.value,
        "started_at": _iso(attempt.started_at),
        "finished_at": _iso(attempt.finished_at),
        "error_message": attempt.error_message,
        "duration_ms": attempt.duration_ms,
        "created_at": _iso(attempt.created_at),
    }


def document_task_dto(task, attempts: list) -> dict:
    """DocumentTask 响应模式（含完整尝试记录）。"""
    return {
        "id": str(task.id),
        "document_id": str(task.document_id),
        "document_version": task.document_version,
        "task_type": task.task_type.value,
        "delete_cycle": task.delete_cycle,
        "status": task.status.value,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "total_items": task.total_items,
        "processed_items": task.processed_items,
        "error_code": task.error_code,
        "error_message": _task_error_message(task),
        "queued_at": _iso(task.queued_at),
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "attempts": [document_task_attempt_dto(a) for a in attempts],
    }


def _is_delete_cleanup_failed(doc) -> bool:
    return (
        doc.status == DocumentStatus.FAILED
        and doc.current_task_type == DocumentTaskType.DELETE_CLEANUP
        and doc.error_code == DELETE_CLEANUP_ERROR_CODE
    )


def _error_message(doc) -> str | None:
    if doc.error_code is None:
        return None
    return ASYNC_ERROR_MESSAGES.get(doc.error_code, doc.error_message or DEFAULT_ERROR_MSG)


def _task_error_message(task) -> str | None:
    if task.error_code is None:
        return None
    return ASYNC_ERROR_MESSAGES.get(task.error_code, task.error_message or DEFAULT_ERROR_MSG)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
