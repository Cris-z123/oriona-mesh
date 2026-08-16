"""chunk 阶段 worker（T053 / data-model.md 草稿片段边界）。

- 读取解析结果文本，按固定大小/重叠切分为草稿片段；
- 草稿仅供流水线中间阶段使用，不得参与检索；
- 草稿写入以 ``attempt_id`` fencing 事务替换该版本全部草稿（重试安全）；
- 阶段完成由统一编排器切换（提交后投递）。
"""

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.models.chunk import DocumentChunkDraft
from app.repositories.chunk_drafts import ChunkDraftRepository
from app.repositories.fencing import FencingError
from app.repositories.parse_results import ParseResultRepository
from app.repositories.processing_leases import ProcessingLeaseRepository
from app.services.document_pipeline import DocumentPipelineOrchestrator, StageResult
from app.services.file_storage import FileStorage, default_file_storage
from app.workers.base import (
    TaskNotRunnableError,
    begin_attempt,
    converge_cancelled,
    load_task_boundaries,
)

WORKER_NAME = "orionamesh-chunk"

# 草稿片段策略版本（正式 chunks 元数据与清理使用）。
CHUNK_POLICY_VERSION = "v1"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按字符固定大小切分（重叠窗口保留上下文边界）。"""
    step = max(size - overlap, 1)
    pieces = [text[i : i + size] for i in range(0, len(text), step)]
    return [piece for piece in pieces if piece.strip()] or ([""] if text else [])


def process_chunk(
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
        # 名额必须持续持有；租约缺失说明恢复扫描器已接管。
        if ProcessingLeaseRepository(session).find_open(document_id) is None:
            session.rollback()
            converge_cancelled(session, attempt_id=attempt.id)
            return
        result = ParseResultRepository(session).latest_for_version(
            user_id, knowledge_base_id, document_id, document_version
        )
        if result is None:
            raise RuntimeError("parse result missing for chunk stage")
        text = file_storage.read_object(result.content_object_key).decode("utf-8")
        pieces = _chunk_text(text)
        drafts = [
            DocumentChunkDraft(
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version=document_version,
                seq=index,
                content=piece,
            )
            for index, piece in enumerate(pieces)
        ]
        ChunkDraftRepository(session).replace_for_version(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            drafts=drafts,
        )
        orchestrator.complete_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            result=StageResult(total_items=len(drafts), processed_items=len(drafts)),
        )
    except FencingError:
        session.rollback()
        converge_cancelled(session, attempt_id=attempt.id)
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


def register_tasks(celery_app) -> None:
    """注册 Celery 任务（提交后投递适配层）。"""

    @celery_app.task(name="orionamesh.document_chunk", bind=True)
    def chunk_task(self, task_id: str) -> None:
        from app.infrastructure.database.session import SessionLocal

        session = SessionLocal()
        try:
            bounds = load_task_boundaries(session, uuid.UUID(task_id))
            if bounds is None:
                return
            user_id, knowledge_base_id, document_id, document_version = bounds
            process_chunk(
                session,
                task_id=uuid.UUID(task_id),
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version=document_version,
            )
        finally:
            session.close()

