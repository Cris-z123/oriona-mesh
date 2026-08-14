"""认证路由（T022 / openapi.yaml auth 段）。

- 注册返回 201 UserEnvelope（不含令牌）；
- 登录返回 201、刷新返回 200 SessionEnvelope；
- 登出使用 Bearer 当前用户 + 请求体 refresh token，幂等撤销属于当前用户的会话。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.dependencies.auth import get_current_user
from app.api.v1.schemas.auth import (
    LoginInput,
    RefreshSessionInput,
    RegisterInput,
    session_tokens_dto,
)
from app.api.v1.schemas.common import success_response
from app.api.v1.schemas.users import user_dto
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.services.auth_service import AuthService

router = APIRouter()


@router.post("/users", status_code=201)
def register(payload: RegisterInput, db: Session = Depends(get_db)) -> dict:
    user = AuthService(db).register(payload.email, payload.password, payload.display_name)
    return success_response(user_dto(user)).model_dump(mode="json")


@router.post("/auth/sessions", status_code=201)
def login(payload: LoginInput, db: Session = Depends(get_db)) -> dict:
    _user, tokens = AuthService(db).login(payload.email, payload.password)
    return success_response(session_tokens_dto(tokens)).model_dump(mode="json")


@router.put("/auth/sessions")
def refresh(payload: RefreshSessionInput, db: Session = Depends(get_db)) -> dict:
    tokens = AuthService(db).refresh(payload.refresh_token)
    return success_response(session_tokens_dto(tokens)).model_dump(mode="json")


@router.delete("/auth/sessions")
def logout(
    payload: RefreshSessionInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    AuthService(db).logout(current_user, payload.refresh_token)
    return success_response(None).model_dump(mode="json")
