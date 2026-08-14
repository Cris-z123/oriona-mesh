"""用户响应模式（openapi.yaml User）。"""

from app.models.user import User


def user_dto(user: User) -> dict:
    """User 响应模式；不含密码哈希等敏感字段。"""
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
    }
