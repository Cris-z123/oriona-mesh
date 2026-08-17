"""parse 阶段 worker（T051 / T056 编排）。

- 首次进入 processing 时以数据库事务原子获取资料级处理名额；名额不足保持
  queued（由恢复扫描器/重投重试）；重复投递由租约唯一索引幂等跳过；
- 解析在安全包装（超时/解压上限）内执行，不持有数据库事务；
- 空文本持久化 ``20010``、损坏/不可解析持久化 ``20001``；解析结果以
  ``attempt_id`` fencing 事务写入，随后由统一编排器切换下一阶段；
- 未归类异常按任务重试预算恢复（与模型网关重试相互独立）。
"""

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from app.api.v1.schemas.documents import ASYNC_ERROR_MESSAGES
from app.core.settings import Settings, get_settings
from app.models.document import Document
from app.models.enums import DocumentStatus
from app.repositories.fencing import FencingError
from app.repositories.parse_results import ParseResultRepository
from app.repositories.processing_leases import ProcessingLeaseRepository
from app.services.document_pipeline import DocumentPipelineOrchestrator, StageResult
from app.services.file_storage import FileStorage, default_file_storage
from app.services.parsers import get_parser
from app.services.parsers.base import ParseError
from app.services.parsers.security import parse_safely
from app.workers.base import (
    TaskNotRunnableError,
    begin_attempt,
    converge_cancelled,
    load_task_boundaries,
)

logger = structlog.get_logger()

WORKER_NAME = "orionamesh-parse"


def process_parse(
    session: Session,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    document_version: int,
    file_storage: FileStorage | None = None,
    dispatch: Callable[[str, tuple], None] | None = None,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    file_storage = file_storage or default_file_storage()
    orchestrator = DocumentPipelineOrchestrator(session, dispatch=dispatch)
    leases = ProcessingLeaseRepository(session)

    document = session.get(Document, document_id)
    if document is None:
        return
    if document.status not in (DocumentStatus.QUEUED, DocumentStatus.PROCESSING):
        return  # pending 初始任务不可执行；终态/删除态不执行

    # 资料级处理名额（数据库事务真相源；跨阶段持续持有）。
    if leases.find_open(document_id) is None:
        lease = leases.acquire(
            user_id=user_id,
            document_id=document_id,
            task_id=task_id,
            lease_seconds=settings.storage.processing_lease_seconds,
            max_per_user=settings.storage.processing_max_per_user,
        )
        if lease is None:
            return  # 名额不足：保持 queued，等待名额回收后由扫描器重投
        if document.status == DocumentStatus.QUEUED:
            document.status = DocumentStatus.PROCESSING
            if document.processing_started_at is None:
                document.processing_started_at = datetime.now(UTC)
        session.commit()

    try:
        task, attempt = begin_attempt(
            session,
            task_id=task_id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            worker_name=WORKER_NAME,
        )
    except TaskNotRunnableError:
        return

    try:
        raw = file_storage.read_object(document.storage_path)
        output = parse_safely(
            get_parser(document.file_type),
            raw,
            timeout_seconds=settings.storage.parse_timeout_seconds,
            max_expanded_bytes=settings.storage.parse_max_expanded_bytes,
        )
    except ParseError as exc:
        orchestrator.fail_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            error_code=exc.code,
            error_message=exc.message,
        )
        return
    except Exception:
        session.rollback()
        orchestrator.fail_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            error_code=None,
        )
        return

    # 解析结果对象先写（外部 I/O，不持事务）；数据库写入经 fencing 才算提交。
    content_key = f"parse/{document_id}/v{document_version}"
    normalized_bytes = output.normalized_text.encode("utf-8")
    try:
        file_storage.write_object(content_key, normalized_bytes)
    except Exception:
        # 文件持久化失败：立即收敛 20011（data-model 文件持久化失败），
        # 不得让 attempt 停留在 running 等待租约过期。
        session.rollback()
        orchestrator.fail_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            error_code=20011,
            error_message=ASYNC_ERROR_MESSAGES[20011],
        )
        return
    try:
        ParseResultRepository(session).save(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            content_object_key=content_key,
            content_hash=hashlib.sha256(normalized_bytes).hexdigest(),
            parser_name=output.parser_name,
            parser_version=output.parser_version,
            normalized_chars=len(output.normalized_text),
        )
        orchestrator.complete_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            result=StageResult(total_items=1, processed_items=1),
        )
    except FencingError:
        # fencing 拒绝写入：清理刚写入的解析对象（无行引用，避免存储泄漏）。
        file_storage.delete_object(content_key)
        session.rollback()
        converge_cancelled(session, attempt_id=attempt.id)
    except Exception:
        # 数据库保存/阶段提交失败（非 fencing）：行随回滚消失，解析对象已无
        # 引用，必须清理；否则成为 delete_cleanup 无法发现的无主派生对象。
        try:
            file_storage.delete_object(content_key)
        except Exception:  # noqa: BLE001 - 清理失败不阻断失败收敛
            logger.warning(
                "parse_object_cleanup_failed",
                object_key=content_key,
                attempt_id=str(attempt.id),
            )
        session.rollback()
        orchestrator.fail_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            error_code=None,
        )


def register_tasks(celery_app) -> None:
    """注册 Celery 任务（提交后投递适配层）。"""

    @celery_app.task(name="orionamesh.document_parse", bind=True)
    def parse_task(self, task_id: str) -> None:
        from app.infrastructure.database.session import SessionLocal

        session = SessionLocal()
        try:
            bounds = load_task_boundaries(session, uuid.UUID(task_id))
            if bounds is None:
                return
            user_id, knowledge_base_id, document_id, document_version = bounds
            process_parse(
                session,
                task_id=uuid.UUID(task_id),
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version=document_version,
            )
        finally:
            session.close()
