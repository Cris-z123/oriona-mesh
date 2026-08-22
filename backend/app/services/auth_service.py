"""认证服务（T023 / FR-001、FR-002）。

- 注册和登录复用 T017 邮箱规范化函数；注册保持 ``last_login_at=NULL``，登录后更新；
- 刷新轮换在同一数据库事务中按摘要锁定旧 session 并复查有效性：只允许一个并发请求
  撤销旧会话并创建单一后继，后到请求返回 ``10006/401``；无效/过期/已撤销/重放的
  refresh token 只拒绝本次刷新，不得撤销该用户其他 active sessions；
- 登出以 Bearer 用户 + 请求体 refresh token 定位并幂等写 ``revoked_at``；属于当前
  用户的已撤销/过期会话重复删除仍成功，无法匹配/跨用户映射 ``10006/401``。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.middleware.errors import ApiError
from app.api.v1.schemas.common import VALIDATION_ERROR_MSG
from app.core.password_policy import is_valid_registration_password
from app.core.security import (
    ACCESS_TOKEN_TTL_SECONDS,
    REFRESH_TOKEN_LIFETIME_DAYS,
    create_access_token,
    generate_refresh_token,
    hash_password,
    normalize_email,
    refresh_token_hash,
    verify_password,
)
from app.core.settings import get_settings
from app.models.auth_session import AuthSession
from app.models.user import User

_EMAIL_ALREADY_EXISTS_MSG = "该邮箱已注册，请直接登录"
_EMAIL_ALREADY_EXISTS_STATUS = 409
_EMAIL_ALREADY_EXISTS_CODE = 20006

_INVALID_CREDENTIALS_MSG = "邮箱或密码错误"
_INVALID_CREDENTIALS_STATUS = 401
_INVALID_CREDENTIALS_CODE = 10004

_INVALID_REFRESH_TOKEN_MSG = "登录状态已失效，请重新登录"
_INVALID_REFRESH_TOKEN_STATUS = 401
_INVALID_REFRESH_TOKEN_CODE = 10006


class AuthService:
    """注册、登录、刷新轮换与登出。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # 注册 / 登录
    # ------------------------------------------------------------------
    def register(self, email: str, password: str, display_name: str | None = None) -> User:
        """注册用户并保持 ``last_login_at=NULL``。

        契约（openapi.yaml）中注册只返回用户、不返回令牌：客户端随后调用登录获取
        会话令牌，因此注册不创建登录会话，避免孤儿 session 行。
        """
        if not is_valid_registration_password(password):
            # API 入口由 Pydantic 拒绝；这里保留服务层防御，避免内部调用绕过 FR-001。
            raise ApiError(10003, VALIDATION_ERROR_MSG, 400)
        normalized = _normalize_or_400(email)
        existing = self.session.scalar(select(User).where(User.email == normalized))
        if existing is not None:
            raise ApiError(
                _EMAIL_ALREADY_EXISTS_CODE, _EMAIL_ALREADY_EXISTS_MSG, _EMAIL_ALREADY_EXISTS_STATUS
            )
        user = User(
            email=normalized,
            password_hash=hash_password(password),
            display_name=display_name,
            last_login_at=None,  # 注册保持 NULL
        )
        self.session.add(user)
        try:
            self.session.flush()
        except IntegrityError as exc:
            # 并发注册同名邮箱：唯一约束兜底，语义与预检查一致。
            self.session.rollback()
            raise ApiError(
                _EMAIL_ALREADY_EXISTS_CODE, _EMAIL_ALREADY_EXISTS_MSG, _EMAIL_ALREADY_EXISTS_STATUS
            ) from exc
        self.session.commit()
        return user

    def login(self, email: str, password: str) -> tuple[User, dict]:
        try:
            normalized = normalize_email(email)
        except ValueError:
            # 格式非法的邮箱不可能匹配任何账号：按凭证错误处理，不泄露格式规则。
            raise ApiError(
                _INVALID_CREDENTIALS_CODE, _INVALID_CREDENTIALS_MSG, _INVALID_CREDENTIALS_STATUS
            ) from None
        user = self.session.scalar(select(User).where(User.email == normalized))
        if user is None or not verify_password(password, user.password_hash):
            raise ApiError(
                _INVALID_CREDENTIALS_CODE, _INVALID_CREDENTIALS_MSG, _INVALID_CREDENTIALS_STATUS
            )
        user.last_login_at = datetime.now(UTC)
        tokens = self._issue_tokens(user.id)
        self.session.commit()
        return user, tokens

    # ------------------------------------------------------------------
    # 刷新轮换
    # ------------------------------------------------------------------
    def refresh(self, refresh_token: str) -> dict:
        token_hash = refresh_token_hash(refresh_token)
        # 行锁保证同一 token 的并发轮换串行：先到者撤销并创建单一后继，后到者观察到
        # 已撤销并返回 10006/401（READ COMMITTED 下 FOR UPDATE 重读已提交版本）。
        session_row = self.session.scalar(
            select(AuthSession)
            .where(AuthSession.refresh_token_hash == token_hash)
            .with_for_update()
        )
        if session_row is None or not self._is_valid(session_row):
            raise ApiError(
                _INVALID_REFRESH_TOKEN_CODE,
                _INVALID_REFRESH_TOKEN_MSG,
                _INVALID_REFRESH_TOKEN_STATUS,
            )
        session_row.revoked_at = datetime.now(UTC)
        tokens = self._issue_tokens(session_row.user_id, rotated_from=session_row.id)
        self.session.commit()
        return tokens

    # ------------------------------------------------------------------
    # 登出
    # ------------------------------------------------------------------
    def logout(self, user: User, refresh_token: str) -> None:
        token_hash = refresh_token_hash(refresh_token)
        session_row = self.session.scalar(
            select(AuthSession).where(
                AuthSession.refresh_token_hash == token_hash,
                AuthSession.user_id == user.id,
            )
        )
        if session_row is None:
            raise ApiError(
                _INVALID_REFRESH_TOKEN_CODE,
                _INVALID_REFRESH_TOKEN_MSG,
                _INVALID_REFRESH_TOKEN_STATUS,
            )
        if session_row.revoked_at is None:
            # 幂等写入：已撤销/已过期且属于当前用户时仍成功。
            session_row.revoked_at = datetime.now(UTC)
        self.session.commit()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _issue_tokens(self, user_id: uuid.UUID, rotated_from: uuid.UUID | None = None) -> dict:
        access_token = create_access_token(str(user_id), get_settings().auth_jwt_secret_key_value)
        refresh_token = generate_refresh_token()
        session_row = AuthSession(
            user_id=user_id,
            refresh_token_hash=refresh_token_hash(refresh_token),
            rotated_from_session_id=rotated_from,
            expires_at=datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS),
        )
        self.session.add(session_row)
        self.session.flush()
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        }

    @staticmethod
    def _is_valid(session_row: AuthSession) -> bool:
        if session_row.revoked_at is not None:
            return False
        if session_row.expires_at <= datetime.now(UTC):
            return False
        return True


def _normalize_or_400(email: str) -> str:
    try:
        return normalize_email(email)
    except ValueError:
        raise ApiError(10003, VALIDATION_ERROR_MSG, 400) from None
