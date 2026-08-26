"""embed 阶段 worker（T054 / data-model.md 阶段切换与片段写入）。

- 外部模型调用不持有数据库事务；向量取得后经 :class:`ChunkRepository` 以
  ``attempt_id`` fencing 在同一事务按唯一逻辑键批量直写正式 ``chunks``，
  支持重试安全批次（先清后写原子替换，崩溃后重跑幂等）；
- 心跳在外部调用间隙续租；资料进入 ``deleting`` 后心跳被拒并收敛取消；
- 嵌入失败持久化 ``20012``；未归类异常按任务重试预算恢复；
- 不在此阶段翻转资料为 ``completed``（发布由 finalize 校验）。
"""

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.models.chunk import Chunk
from app.repositories.chunk_drafts import ChunkDraftRepository
from app.repositories.chunks import ChunkRepository
from app.repositories.fencing import FencingError
from app.repositories.processing_leases import ProcessingLeaseRepository
from app.services.document_pipeline import DocumentPipelineOrchestrator, StageResult
from app.services.llm.embeddings import (
    EmbeddingFailure,
    EmbeddingService,
    default_embedding_service,
)
from app.workers.base import (
    TaskNotRunnableError,
    begin_attempt,
    converge_cancelled,
    execute_document_task,
)

WORKER_NAME = "orionamesh-embed"

# 每次网关调用处理的草稿数量（外部调用不持事务，逐批 fencing 写入）。
EMBED_BATCH_SIZE = 32


def process_embed(
    session: Session,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    knowledge_base_id: uuid.UUID,
    document_id: uuid.UUID,
    document_version: int,
    embeddings: EmbeddingService | None = None,
    dispatch: Callable[[str, tuple], None] | None = None,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    orchestrator = DocumentPipelineOrchestrator(session, dispatch=dispatch)
    embeddings = embeddings or default_embedding_service()
    leases = ProcessingLeaseRepository(session)

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

    drafts = ChunkDraftRepository(session).list_for_version(
        user_id, knowledge_base_id, document_id, document_version
    )
    if not drafts:
        orchestrator.fail_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            error_code=None,
        )
        return

    model = settings.model_gateway.embedding_model
    chunks: list[Chunk] = []
    try:
        for start in range(0, len(drafts), EMBED_BATCH_SIZE):
            batch = drafts[start : start + EMBED_BATCH_SIZE]
            # 心跳（资料 deleting 后不续租 → 收敛取消）。
            lease = leases.find_open(document_id)
            if lease is None or not leases.heartbeat(
                lease.id,
                document_id,
                task_id,
                settings.storage.processing_lease_seconds,
            ):
                session.rollback()
                converge_cancelled(session, attempt_id=attempt.id)
                return
            session.commit()
            vectors = embeddings.embed_texts([draft.content for draft in batch], user_id=user_id)
            # 跨批次累积，全部取得后在同一 fencing 事务一次性直写正式片段：
            # 逐批调用 replace_for_version 会整版本先删后写，覆盖先前批次。
            chunks.extend(
                Chunk(
                    user_id=user_id,
                    knowledge_base_id=knowledge_base_id,
                    document_id=document_id,
                    document_version=document_version,
                    seq=draft.seq,
                    content=draft.content,
                    embedding=vector,
                    embedding_model=model,
                    policy_version="v1",
                    page=draft.page,
                    section=draft.section,
                )
                for draft, vector in zip(batch, vectors, strict=True)
            )
    except EmbeddingFailure:
        session.rollback()
        orchestrator.fail_stage(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            error_code=20012,
            error_message="资料向量化失败，请删除后重新上传",
        )
        return
    except FencingError:
        session.rollback()
        converge_cancelled(session, attempt_id=attempt.id)
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

    try:
        # 全部批次取得后同一 fencing 事务一次性直写正式片段（幂等：先清后写原子替换）。
        ChunkRepository(session).replace_for_version(
            attempt_id=attempt.id,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            document_version=document_version,
            chunks=chunks,
        )
        task.processed_items = len(chunks)
        session.commit()
    except FencingError:
        session.rollback()
        converge_cancelled(session, attempt_id=attempt.id)
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

    orchestrator.complete_stage(
        attempt_id=attempt.id,
        user_id=user_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        document_version=document_version,
        result=StageResult(total_items=len(drafts), processed_items=len(chunks)),
    )


def register_tasks(celery_app) -> None:
    """注册 Celery 任务（提交后投递适配层）。"""

    @celery_app.task(name="orionamesh.document_embed", bind=True)
    def embed_task(self, task_id: str | uuid.UUID) -> None:
        execute_document_task(task_id, process_embed)
