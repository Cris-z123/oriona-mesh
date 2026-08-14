"""当前用户路由（T022 / openapi.yaml users/me 段）。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.dependencies.auth import get_current_user
from app.api.v1.schemas.auth import UpdateProfileInput
from app.api.v1.schemas.common import success_response
from app.api.v1.schemas.users import user_dto
from app.infrastructure.database.session import get_db
from app.models.user import User
from app.services.user_service import UserService

router = APIRouter()


@router.get("/users/me")
def get_me(current_user: User = Depends(get_current_user)) -> dict:
    return success_response(user_dto(current_user)).model_dump(mode="json")


@router.patch("/users/me")
def update_me(
    payload: UpdateProfileInput,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    user = UserService(db).update_profile(current_user, payload.display_name)
    return success_response(user_dto(user)).model_dump(mode="json")
