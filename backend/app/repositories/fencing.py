"""持久化写入 fencing 守卫（data-model.md 持久化写入边界）。

解析结果、草稿片段、正式 chunks、checkpoint 与阶段结果引用的仓储写方法必须接收
``attempt_id``；每次写入在同一数据库事务中锁定 attempt、task 与 document，并校验
attempt/task 均为 ``running``、版本一致且资料不为 ``deleting/deleted``。条件不满足
时整笔写入失败（:class:`FencingError`），worker 将当前执行收敛为取消，不得绕过
仓储重试。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.enums import DocumentAttemptStatus, DocumentStatus, DocumentTaskStatus


class FencingError(Exception):
    """fencing 校验失败：禁止写入，worker 收敛为取消。"""


def validate_attempt_write(
    session: Session,
    *,
    attempt_id: uuid.UUID,
    user_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    document_version: int,
    task_type=None,
) -> tuple[DocumentTaskAttempt, DocumentTask, Document]:
    """锁定并校验 attempt/task/document 后返回三者（调用方保持同一事务）。"""
    attempt = session.scalar(
        select(DocumentTaskAttempt)
        .where(DocumentTaskAttempt.id == attempt_id)
        .with_for_update()
    )
    if attempt is None or attempt.status != DocumentAttemptStatus.RUNNING:
        raise FencingError("attempt is not running")
    if attempt.user_id != user_id or attempt.document_id != document_id:
        raise FencingError("attempt boundary mismatch")
    if attempt.document_version != document_version:
        raise FencingError("attempt version mismatch")
    if attempt.knowledge_base_id != knowledge_base_id:
        raise FencingError("attempt knowledge base mismatch")

    task = session.scalar(
        select(DocumentTask).where(DocumentTask.id == attempt.task_id).with_for_update()
    )
    if task is None or task.status != DocumentTaskStatus.RUNNING:
        raise FencingError("task is not running")
    if task_type is not None and task.task_type != task_type:
        raise FencingError("task type mismatch")

    document = session.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    if document is None:
        raise FencingError("document not found")
    if document.status in (DocumentStatus.DELETING, DocumentStatus.DELETED):
        raise FencingError("document is being deleted")
    if document.version != document_version:
        raise FencingError("document version mismatch")
    return attempt, task, document
