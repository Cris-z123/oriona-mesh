"""启动配置校验单元测试（T024/T026 / I4 审查项）。

覆盖：缺少关键变量、JWT 密钥长度、密钥隔离、部署环境拒绝回退默认值、
streaming 失联上限预算校验与非法配置值拒绝。
"""

import pytest
from pydantic import SecretStr
from pydantic_core import ValidationError

from app.core.readiness import assert_startup_config, validate_config
from app.core.settings import Settings


def _settings(**overrides) -> Settings:
    base: dict = {
        "auth_jwt_secret_key": SecretStr("k" * 40),
        "rate_limit": {"subject_hmac_key": SecretStr("r" * 40)},
        "model_gateway": {
            "endpoint": "https://api.example.com/v1",
            "api_key": SecretStr("m" * 40),
            "query_rewrite_model": "qw",
            "generation_model": "gen",
        },
    }
    return Settings(**{**base, **overrides})


class TestValidateConfig:
    def test_complete_config_passes(self) -> None:
        assert validate_config(_settings()) == []

    def test_missing_jwt_key_reported(self) -> None:
        errors = validate_config(_settings(auth_jwt_secret_key=SecretStr("")))
        assert any("AUTH_JWT_SECRET_KEY is required" in e for e in errors)

    def test_short_jwt_key_reported(self) -> None:
        errors = validate_config(_settings(auth_jwt_secret_key=SecretStr("short")))
        assert any("at least 32 UTF-8 bytes" in e for e in errors)

    def test_missing_subject_key_reported(self) -> None:
        errors = validate_config(_settings(rate_limit={"subject_hmac_key": SecretStr("")}))
        assert any("RATE_LIMIT_SUBJECT_HMAC_KEY is required" in e for e in errors)

    def test_missing_gateway_vars_reported(self) -> None:
        errors = validate_config(
            _settings(
                model_gateway={
                    "endpoint": None,
                    "api_key": SecretStr(""),
                    "query_rewrite_model": None,
                    "generation_model": None,
                }
            )
        )
        joined = " ".join(errors)
        assert "MODEL_GATEWAY_ENDPOINT is required" in joined
        assert "MODEL_GATEWAY_API_KEY is required" in joined
        assert "MODEL_GATEWAY_QUERY_REWRITE_MODEL is required" in joined
        assert "MODEL_GATEWAY_GENERATION_MODEL is required" in joined

    def test_secret_reuse_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            _settings(
                auth_jwt_secret_key=SecretStr("same-secret-32-bytes-long!!"),
                rate_limit={"subject_hmac_key": SecretStr("same-secret-32-bytes-long!!")},
            )

    def test_streaming_stale_budget_check(self) -> None:
        # 预算不足的 streaming 失联上限必须被拒绝。
        settings = _settings(
            retrieval={"message_streaming_stale_seconds": 60},
            model_gateway={
                "endpoint": "https://api.example.com/v1",
                "api_key": SecretStr("m" * 40),
                "query_rewrite_model": "qw",
                "generation_model": "gen",
                "query_rewrite_max_retries": 5,
                "generation_max_retries": 5,
            },
        )
        errors = validate_config(settings)
        assert any("MESSAGE_STREAMING_STALE_SECONDS" in e for e in errors)


class TestDeploymentRejection:
    def test_deployment_requires_explicit_injection(self, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("DOCUMENT_STORAGE_ROOT", raising=False)
        monkeypatch.delenv("AUTH_JWT_SECRET_KEY", raising=False)
        errors = validate_config(_settings(app_env="production"))
        joined = " ".join(errors)
        assert "DATABASE_URL must be explicitly injected" in joined
        assert "REDIS_URL must be explicitly injected" in joined
        assert "DOCUMENT_STORAGE_ROOT must be explicitly injected" in joined

    def test_development_allows_defaults(self, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("DOCUMENT_STORAGE_ROOT", raising=False)
        errors = validate_config(_settings(app_env="development"))
        assert not any("must be explicitly injected" in e for e in errors)


class TestAssertStartupConfig:
    def test_missing_config_raises_system_exit(self) -> None:
        with pytest.raises(SystemExit) as exc:
            assert_startup_config(_settings(auth_jwt_secret_key=SecretStr("")))
        assert "startup configuration failed" in str(exc.value)
        assert "AUTH_JWT_SECRET_KEY is required" in str(exc.value)

    def test_valid_config_does_not_raise(self) -> None:
        assert_startup_config(_settings())


class TestInvalidValuesRejected:
    def test_invalid_endpoint_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            _settings(model_gateway={"endpoint": "not-a-url"})

    def test_unknown_provider_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            _settings(model_gateway={"provider": "unknown"})

    def test_http_non_loopback_endpoint_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(model_gateway={"endpoint": "http://api.example.com/v1"})

    def test_out_of_range_rate_limit_window_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _settings(
                rate_limit={"subject_hmac_key": SecretStr("r" * 40), "auth_ip_window_seconds": 0}
            )
