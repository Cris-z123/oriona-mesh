"""``/v1`` 路由器（T022/T048/T066/T074）。

Phase 2 注册认证、当前用户与知识库路由；Phase 3 追加资料路由；Phase 4 追加
会话/消息/引用与 SSE 问答路由。
"""

from fastapi import APIRouter

from app.api.v1.routes import auth, conversations, documents, knowledge_bases, messages, users

api_router = APIRouter(prefix="/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(documents.router)
api_router.include_router(conversations.router)
api_router.include_router(messages.router)
