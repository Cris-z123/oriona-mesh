"""解析安全集成测试（T039 / FR-030、FR-034）。

覆盖 worker 级行为：空/扫描资料持久化 ``20010 EMPTY_DOCUMENT``、损坏资料持久化
``20001`` 并显示固定安全提示；失败后不创建解析结果、草稿片段或正式片段；成功解析
写入解析结果并幂等激活下一阶段（chunk）。需要真实 PostgreSQL。
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.infrastructure.storage.local import LocalStorage
from app.models.chunk import Chunk, DocumentChunkDraft, DocumentParseResult
from app.models.document import Document
from app.models.document_task import DocumentTask
from app.models.enums import DocumentStatus, DocumentTaskStatus
from app.services.document_service import DocumentService
from app.services.file_storage import FileStorage
from app.workers.document_parse import process_parse

pytestmark = pytest.mark.integration

_EMPTY_DOC_MSG = "资料内容为空，请删除后重新上传"
_PARSE_FAILED_MSG = "资料解析失败，请删除后重新上传"


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    return FileStorage(LocalStorage(tmp_path / "store"))


@pytest.fixture
def dispatch_calls():
    calls: list[tuple[str, tuple]] = []

    def fake(name: str, args: tuple) -> None:
        calls.append((name, args))

    return fake, calls


def _seed_queued_document(
    db_session: Session,
    storage: FileStorage,
    dispatch,
    filename: str,
    content: bytes,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """经完整上传流程创建 queued 资料；返回 (user_id, kb_id, doc_id)。"""
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User

    user = User(email=f"parse-{uuid.uuid4()}@example.com", password_hash="x" * 60)
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db_session.add(kb)
    db_session.commit()

    import io

    from fastapi import UploadFile

    service = DocumentService(db_session, file_storage=storage, dispatch=dispatch)
    outcome = service.upload(
        user.id, kb.id, [UploadFile(file=io.BytesIO(content), filename=filename)]
    )
    doc_id = uuid.UUID(outcome.items[0]["id"])
    return user.id, kb.id, doc_id


class TestParseWorkerTerminalStates:
    def test_empty_pdf_20010_no_derived_objects(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id, doc_id = _seed_queued_document(
            db_session, storage, dispatch, "empty.pdf", _empty_pdf()
        )
        calls.clear()  # 种子上传的投递不计入断言
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        task = db_session.query(DocumentTask).filter_by(document_id=doc_id).one()
        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.refresh(doc)
        db_session.refresh(task)
        # 任务与资料收敛为明确失败，固定安全提示。
        assert task.status == DocumentTaskStatus.FAILED
        assert task.error_code == 20010
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20010
        assert doc.error_message == _EMPTY_DOC_MSG
        assert doc.processing_finished_at is not None
        # 空资料不得生成解析结果、草稿或正式片段。
        assert db_session.query(DocumentParseResult).count() == 0
        assert db_session.query(DocumentChunkDraft).count() == 0
        assert db_session.query(Chunk).count() == 0
        # 不投递后续阶段。
        assert calls == []

    def test_corrupted_pdf_20001(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id, doc_id = _seed_queued_document(
            db_session, storage, dispatch, "broken.pdf", b"%PDF-1.4 this is not a pdf"
        )
        calls.clear()  # 种子上传的投递不计入断言
        doc = db_session.get(Document, doc_id)
        assert doc is not None
        task = db_session.query(DocumentTask).filter_by(document_id=doc_id).one()
        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.refresh(doc)
        db_session.refresh(task)
        assert task.status == DocumentTaskStatus.FAILED
        assert task.error_code == 20001
        assert doc.status == DocumentStatus.FAILED
        assert doc.error_code == 20001
        assert doc.error_message == _PARSE_FAILED_MSG
        assert db_session.query(DocumentParseResult).count() == 0
        assert db_session.query(Chunk).count() == 0
        assert calls == []

    def test_parse_success_persists_result_and_activates_chunk(
        self,
        db_session: Session,
        storage: FileStorage,
        dispatch_calls,
    ) -> None:
        dispatch, calls = dispatch_calls
        user_id, kb_id, doc_id = _seed_queued_document(
            db_session, storage, dispatch, "ok.pdf", _text_pdf("hello parse pipeline")
        )
        calls.clear()  # 种子上传的投递不计入断言
        doc = db_session.query(Document).filter_by(id=doc_id).one()
        task = db_session.query(DocumentTask).filter_by(document_id=doc_id).one()
        process_parse(
            db_session,
            task_id=task.id,
            user_id=user_id,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version=1,
            file_storage=storage,
            dispatch=dispatch,
        )
        db_session.refresh(doc)
        db_session.refresh(task)
        # 当前任务成功，下一阶段幂等创建并排队。
        assert task.status == DocumentTaskStatus.SUCCEEDED
        assert doc.current_task_type is not None
        assert doc.current_task_type.value == "chunk"
        assert doc.status == DocumentStatus.PROCESSING
        chunk_task = db_session.query(DocumentTask).filter_by(task_type="chunk").one()
        assert chunk_task.status == DocumentTaskStatus.QUEUED
        assert chunk_task.retry_count == 0
        assert calls == [("orionamesh.document_chunk", (chunk_task.id,))]
        # 解析结果持久化并携带版本/租户边界。
        result = db_session.query(DocumentParseResult).one()
        assert result.document_version == 1
        assert result.user_id == user_id
        assert result.knowledge_base_id == kb_id
        assert result.normalized_chars > 0
        assert result.parser_name
        assert result.parser_version
        # 解析文本对象已落盘。
        assert storage.read_object(result.content_object_key).startswith(b"hello parse pipeline")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _empty_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()  # 有页面但无文本层（模拟扫描件）
    return doc.tobytes()


def _text_pdf(text: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()
