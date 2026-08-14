"""用户服务（T023 / FR-002）。

查看与更新本人基本资料；不允许读取或修改其他用户资料（当前用户由认证依赖注入）。
"""

from sqlalchemy.orm import Session

from app.models.user import User


class UserService:
    """当前用户资料服务。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def update_profile(self, user: User, display_name: str | None = None) -> User:
        if display_name is not None:
            user.display_name = display_name
        self.session.commit()
        return user
