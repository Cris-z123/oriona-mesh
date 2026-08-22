"""认证请求/响应模式（openapi.yaml auth 段）。

邮箱只做长度约束，格式校验与规范化（strip → 校验 → casefold）在服务层执行，
保证注册、登录与限流复用同一规范化函数。
"""

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.password_policy import is_valid_registration_password


class RegisterInput(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, value: str) -> str:
        if not is_valid_registration_password(value):
            raise ValueError("password must contain letters and digits")
        return value


class LoginInput(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class RefreshSessionInput(BaseModel):
    # 与 openapi.yaml RefreshSessionInput 一致：rt_ + 43 个 Base64URL 字符；
    # 非 ASCII 或格式不符的 token 在参数校验层即拒绝（10003/400），
    # 不进入哈希/指纹计算（避免 UnicodeEncodeError 变成 500）。
    refresh_token: str = Field(min_length=46, max_length=46, pattern=r"^rt_[A-Za-z0-9_-]{43}$")


class UpdateProfileInput(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UpdateProfileInput":
        # OpenAPI minProperties: 1：空 PATCH 必须 10003/400。
        if self.display_name is None:
            raise ValueError("at least one field must be provided")
        return self


def session_tokens_dto(tokens: dict) -> dict:
    """SessionTokens 响应模式。"""
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": "Bearer",
        "expires_in": tokens["expires_in"],
    }
