"""消息发送 SSE 路由（T074 / openapi.yaml messages 段）。

- ``POST /conversations/{id}/messages`` 返回 ``text/event-stream``；会话未命中
  统一 ``20007/404``，知识库无完成资料 ``20005/409``（普通 JSON 信封，非 SSE）；
- 依赖装配：真实端口（会话/检索/改写/生成/引用）经 :class:`AnswerService`
  注入，测试可通过 ``get_message_answer_service`` 覆盖；
- 消息发送走 ``question-user`` 限流策略（RateLimitMiddleware 分类）。
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1.dependencies.auth import get_current_user
from app.api.v1.schemas.conversations import SendMessageInput
from app.api.v1.sse.message_stream import stream_answer_events
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.repositories.base import require_active_knowledge_base
from app.services.answer_service import AnswerService
from app.services.citation_service import CitationService
from app.services.conversation_service import ConversationService
from app.services.llm.chat import GenerationService, QueryRewriteService
from app.services.llm.reranker import RerankerService
from app.services.retrieval_service import RetrievalService

router = APIRouter()


def get_message_answer_service(db: Session = Depends(get_db)) -> AnswerService:
    """默认问答编排装配（真实端口）；测试覆盖本依赖注入假端口。"""
    return AnswerService(
        conversations=ConversationService(db),
        retrieval=RetrievalService(db, reranker=RerankerService()),
        rewrite=QueryRewriteService(),
        generation=GenerationService(),
        citations=CitationService(db),
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    answer: AnswerService = Depends(get_message_answer_service),
) -> StreamingResponse:
    conv, _ = ConversationService(db).get(current_user.id, conversation_id)
    # 知识库已进入 deleting/delete_failed（T081 编排）时拒绝消息发送，20002/404。
    require_active_knowledge_base(db, conv.knowledge_base_id, current_user.id)
    bundle = answer.prepare(
        user_id=current_user.id,
        knowledge_base_id=conv.knowledge_base_id,
        conversation_id=conversation_id,
        content=payload.content,
    )
    return StreamingResponse(
        stream_answer_events(answer, bundle),
        media_type="text/event-stream",
    )
