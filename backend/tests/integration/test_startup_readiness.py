"""启动就绪检查与迁移可运行性集成测试（T085 / quickstart 配置契约）。

对照 quickstart.md 的「基础设施配置契约」「资料处理配置契约」「检索与消息恢复
配置契约」与 ``app/core/readiness.py`` 验证：

- 迁移可运行性：对测试库执行 ``alembic upgrade head``（编程方式），验证迁移成功、
  可重跑且 vector/pg_trgm/pgcrypto 扩展与 ``alembic_version`` 均就位；
- ``validate_config``：合法本机测试环境变量下无错误；AUTH_JWT_SECRET_KEY 缺失或
  UTF-8 编码不足 32 字节、RATE_LIMIT_SUBJECT_HMAC_KEY 缺失、模型网关 endpoint /
  api key / 必填模型缺失时返回错误列表而非抛出；rerank_model 为空不报错；
- 检索阈值越界与 ``MESSAGE_STREAMING_STALE_SECONDS`` 小于全部模型尝试预算 + 60 秒
  被拒绝（等于下限通过）；
- ``check_runtime``：数据库不可达报 database unreachable；本地持久卷根不存在/
  是文件/不可写分别报错；
- ``/ready``：数据库/Redis/存储根就绪时 200 ``code=0``；``DOCUMENT_STORAGE_ROOT``
  指向不存在路径时 503 ``code=50001`` 且信封携带 UUID trace_id；
- ``StorageSettings``（``DOCUMENT_*``）字段名与默认值按契约装配。
"""

import os
import stat
import sys
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_core import ValidationError
from sqlalchemy import create_engine, text

from app.core.readiness import check_runtime, validate_config
from app.core.redis import redis_healthy
from app.core.settings import Settings, get_settings
from app.infrastructure.storage.config import StorageSettings
from app.services.retrieval_config import RetrievalSettings

pytestmark = pytest.mark.integration


def _settings(**overrides) -> Settings:
    """构造配置合法的 Settings 子实例（与 unit/core/test_readiness.py 同构）。"""
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


class TestMigrationsRun:
    """迁移可运行性：alembic upgrade head 成功、可重跑且必需扩展就位（基础设施契约）。"""

    def test_upgrade_head_applies_and_installs_extensions(self, test_engine) -> None:
        backend_root = Path(__file__).resolve().parents[2]
        cfg = Config(str(backend_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_root / "migrations"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        # 清空 schema 保证迁移从头应用（与 conftest 重置方式一致）。
        with test_engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.commit()
        # migrations/env.py 从唯一配置源读取 DATABASE_URL，先刷新缓存。
        get_settings.cache_clear()
        command.upgrade(cfg, "head")
        # 幂等：已处于 head 时再次执行不得报错。
        command.upgrade(cfg, "head")
        with test_engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert version == head
            extensions = {row[0] for row in conn.execute(text("SELECT extname FROM pg_extension"))}
        assert {"vector", "pg_trgm", "pgcrypto"} <= extensions


class TestValidateConfigEnv:
    """环境变量覆盖 + get_settings.cache_clear() 下的 validate_config 行为。"""

    def test_valid_test_env_config_passes(self) -> None:
        get_settings.cache_clear()
        assert validate_config() == []

    def test_missing_jwt_secret_key_reported(self, monkeypatch) -> None:
        monkeypatch.delenv("AUTH_JWT_SECRET_KEY", raising=False)
        get_settings.cache_clear()
        errors = validate_config()
        assert any("AUTH_JWT_SECRET_KEY is required" in e for e in errors)

    def test_short_jwt_secret_key_reported(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTH_JWT_SECRET_KEY", "short")
        get_settings.cache_clear()
        errors = validate_config()
        assert any("AUTH_JWT_SECRET_KEY must be at least 32 UTF-8 bytes" in e for e in errors)

    def test_multibyte_jwt_secret_key_under_32_bytes_reported(self, monkeypatch) -> None:
        # “密” UTF-8 编码占 3 字节：10 个共 30 字节，仍不足 32 字节契约下限。
        monkeypatch.setenv("AUTH_JWT_SECRET_KEY", "密" * 10)
        get_settings.cache_clear()
        errors = validate_config()
        assert any("at least 32 UTF-8 bytes" in e for e in errors)

    def test_missing_subject_hmac_key_reported(self, monkeypatch) -> None:
        monkeypatch.delenv("RATE_LIMIT_SUBJECT_HMAC_KEY", raising=False)
        get_settings.cache_clear()
        errors = validate_config()
        assert any("RATE_LIMIT_SUBJECT_HMAC_KEY is required" in e for e in errors)

    def test_missing_gateway_endpoint_reported(self, monkeypatch) -> None:
        monkeypatch.delenv("MODEL_GATEWAY_ENDPOINT", raising=False)
        get_settings.cache_clear()
        errors = validate_config()
        assert any("MODEL_GATEWAY_ENDPOINT is required" in e for e in errors)

    def test_missing_gateway_api_key_reported(self, monkeypatch) -> None:
        monkeypatch.delenv("MODEL_GATEWAY_API_KEY", raising=False)
        get_settings.cache_clear()
        errors = validate_config()
        assert any("MODEL_GATEWAY_API_KEY is required" in e for e in errors)

    def test_missing_required_gateway_models_reported(self, monkeypatch) -> None:
        monkeypatch.delenv("MODEL_GATEWAY_QUERY_REWRITE_MODEL", raising=False)
        monkeypatch.delenv("MODEL_GATEWAY_GENERATION_MODEL", raising=False)
        get_settings.cache_clear()
        errors = validate_config()
        joined = " ".join(errors)
        assert "MODEL_GATEWAY_QUERY_REWRITE_MODEL is required" in joined
        assert "MODEL_GATEWAY_GENERATION_MODEL is required" in joined

    def test_empty_rerank_model_is_optional(self, monkeypatch) -> None:
        monkeypatch.delenv("MODEL_GATEWAY_RERANK_MODEL", raising=False)
        get_settings.cache_clear()
        assert validate_config() == []


class TestStreamingStaleBudget:
    """MESSAGE_STREAMING_STALE_SECONDS 必须覆盖全部模型尝试预算 + 60 秒（检索契约）。"""

    def test_too_small_stale_seconds_rejected(self, monkeypatch) -> None:
        # 默认预算：改写 (1+1) + 生成 (1+1) = 4，下限 64；63 必须拒绝就绪。
        monkeypatch.setenv("MESSAGE_STREAMING_STALE_SECONDS", "63")
        get_settings.cache_clear()
        errors = validate_config()
        assert any("MESSAGE_STREAMING_STALE_SECONDS" in e for e in errors)

    def test_exact_budget_plus_60_seconds_passes(self, monkeypatch) -> None:
        monkeypatch.setenv("MESSAGE_STREAMING_STALE_SECONDS", "64")
        get_settings.cache_clear()
        assert validate_config() == []

    def test_method_validation_boundary(self) -> None:
        settings = _settings()
        budget = settings.model_gateway.attempt_budget_sum()
        assert budget == 4
        settings.retrieval.validate_streaming_stale(budget)  # 默认 360 通过
        with pytest.raises(ValueError):
            _settings(
                retrieval={"message_streaming_stale_seconds": budget + 59}
            ).retrieval.validate_streaming_stale(budget)
        # 恰好等于预算 + 60 秒必须通过。
        _settings(
            retrieval={"message_streaming_stale_seconds": budget + 60}
        ).retrieval.validate_streaming_stale(budget)


class TestRetrievalThresholds:
    """两个检索阈值必须在闭区间 [0, 1]（检索配置契约）。"""

    def test_vector_similarity_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalSettings(vector_min_similarity=1.01)

    def test_trgm_similarity_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalSettings(trgm_min_similarity=-0.01)

    def test_boundary_values_accepted(self) -> None:
        settings = RetrievalSettings(vector_min_similarity=0.0, trgm_min_similarity=1.0)
        assert settings.vector_min_similarity == 0.0
        assert settings.trgm_min_similarity == 1.0

    def test_env_override_out_of_range_rejected(self, monkeypatch) -> None:
        monkeypatch.setenv("RETRIEVAL_VECTOR_MIN_SIMILARITY", "1.01")
        get_settings.cache_clear()
        with pytest.raises(ValidationError):
            get_settings()


class TestStorageRootChecks:
    """本地持久卷根不存在/不是目录/不可写时不得报告就绪（基础设施契约）。"""

    def test_missing_root_reported(self, tmp_path) -> None:
        settings = _settings(storage={"storage_root": str(tmp_path / "missing-root")})
        errors = check_runtime(settings)
        assert any("storage root does not exist" in e for e in errors)

    def test_root_is_a_file_reported(self, tmp_path) -> None:
        target = tmp_path / "not-a-directory"
        target.write_text("x", encoding="utf-8")
        settings = _settings(storage={"storage_root": str(target)})
        errors = check_runtime(settings)
        assert any("storage root is not a directory" in e for e in errors)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows 目录只读属性不影响创建文件，只读模拟不可靠",
    )
    def test_unwritable_root_reported(self, tmp_path) -> None:
        root = tmp_path / "readonly-root"
        root.mkdir()
        root.chmod(
            stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        )
        try:
            settings = _settings(storage={"storage_root": str(root)})
            errors = check_runtime(settings)
            assert any("storage root not writable" in e for e in errors)
        finally:
            root.chmod(stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)


class TestRuntimeChecks:
    """check_runtime：数据库不可达与数据库/扩展就绪分支。"""

    def test_database_unreachable_reported(self, tmp_path) -> None:
        bad_engine = create_engine(
            "postgresql+psycopg://orionamesh:orionamesh@127.0.0.1:59999/orionamesh",
            connect_args={"connect_timeout": 2},
        )
        try:
            errors = check_runtime(
                _settings(storage={"storage_root": str(tmp_path)}), engine=bad_engine
            )
        finally:
            bad_engine.dispose()
        assert any("database unreachable" in e for e in errors)

    def test_reachable_database_with_extensions_passes(self, tmp_path) -> None:
        errors = check_runtime(_settings(storage={"storage_root": str(tmp_path)}))
        assert not any("database" in e or "extension" in e for e in errors)


class TestReadyEndpoint:
    """/ready：就绪时 200 code=0；存储根缺失时 503 code=50001 且信封含 UUID trace_id。"""

    def test_ready_returns_200_when_services_available(
        self, client: TestClient, test_engine
    ) -> None:
        if not redis_healthy():
            pytest.skip("redis unavailable")
        resp = client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert body["msg"] == ""
        assert body["data"] == {"ready": True}
        uuid.UUID(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]

    def test_ready_503_when_storage_root_missing(
        self, client: TestClient, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("DOCUMENT_STORAGE_ROOT", str(tmp_path / "missing-root"))
        get_settings.cache_clear()
        try:
            resp = client.get("/ready")
        finally:
            get_settings.cache_clear()
        assert resp.status_code == 503
        body = resp.json()
        assert body["code"] == 50001
        assert body["data"]["ready"] is False
        assert any("storage root does not exist" in e for e in body["data"]["errors"])
        uuid.UUID(body["trace_id"])
        assert resp.headers["X-Trace-Id"] == body["trace_id"]


class TestStorageSettingsAssembly:
    """StorageSettings（DOCUMENT_*）字段名与默认值按资料处理配置契约装配。"""

    def test_defaults_match_contract(self) -> None:
        settings = StorageSettings()
        assert settings.storage_root == os.environ["DOCUMENT_STORAGE_ROOT"]
        assert settings.processing_max_per_user == 3
        assert settings.processing_lease_seconds == 300
        assert settings.upload_pending_timeout_seconds == 300
        assert settings.parse_timeout_seconds == 60
        assert settings.parse_max_expanded_bytes == 209_715_200
        assert settings.upload_idempotency_ttl_seconds == 86_400

    def test_root_settings_assembles_storage(self) -> None:
        settings = get_settings()
        assert isinstance(settings.storage, StorageSettings)
        assert settings.storage.processing_max_per_user == 3

    def test_env_override_assembled(self, monkeypatch) -> None:
        monkeypatch.setenv("DOCUMENT_PROCESSING_MAX_PER_USER", "5")
        get_settings.cache_clear()
        try:
            assert get_settings().storage.processing_max_per_user == 5
        finally:
            get_settings.cache_clear()
