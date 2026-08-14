"""领域 ORM 模型包。

导入本包即注册全部表到 ``app.db.base.Base.metadata``，Alembic 的
``target_metadata`` 与测试建表共用同一元数据。
"""

from app.models.auth_session import AuthSession
from app.models.chunk import Chunk, DocumentChunkDraft, DocumentParseResult
from app.models.conversation import Conversation, Message, MessageCitation
from app.models.document import Document
from app.models.document_task import DocumentTask, DocumentTaskAttempt
from app.models.knowledge_base import KnowledgeBase
from app.models.processing_lease import DocumentProcessingLease
from app.models.upload_request import DocumentUploadRequest
from app.models.user import User

__all__ = [
    "AuthSession",
    "Chunk",
    "Conversation",
    "Document",
    "DocumentChunkDraft",
    "DocumentParseResult",
    "DocumentProcessingLease",
    "DocumentTask",
    "DocumentTaskAttempt",
    "DocumentUploadRequest",
    "KnowledgeBase",
    "Message",
    "MessageCitation",
    "User",
]
