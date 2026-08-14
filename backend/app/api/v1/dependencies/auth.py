"""当前用户认证依赖（T019 / FR-001）。

Bearer 解析 + Access Token 验证（T017）：缺失、格式错误、签名或算法无效、必填声明
错误、type 错误或过期统一返回 ``10001/401`` 与“请重新登录”；``10004`` 仅用于登录
邮箱或密码不匹配。
"""

import re
import uuid

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import TOKEN_INVALID_MSG
from app.core.security import decode_access_token
from app.core.settings import get_settings
from app.infrastructure.database.session import get_db
from app.models.user import User

_TOKEN_INVALID_MSG = TOKEN_INVALID_MSG
_TOKEN_INVALID_CODE = 10001
_TOKEN_INVALID_STATUS = 401

_AUTH_BEARER_RE = re.compile(r"^Bearer\s+(\S+)$", re.IGNORECASE)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """解析并验证 Bearer Access Token，返回当前用户；全部失败统一 10001/401。"""
    auth = request.headers.get("authorization")
    if not auth:
        raise _invalid_token()
    match = _AUTH_BEARER_RE.match(auth)
    if not match:
        raise _invalid_token()
    sub = decode_access_token(match.group(1), get_settings().auth_jwt_secret_key_value)
    try:
        user_id = uuid.UUID(sub)
    except (ValueError, AttributeError):
        raise _invalid_token() from None
    user = db.get(User, user_id)
    if user is None:
        # 用户已不存在（如账号删除）：同样按令牌无效处理，不泄露资源信息。
        raise _invalid_token()
    return user


def _invalid_token() -> ApiError:
    return ApiError(_TOKEN_INVALID_CODE, _TOKEN_INVALID_MSG, _TOKEN_INVALID_STATUS)
