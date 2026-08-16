"""设置装配单元测试：.env 文件必须覆盖嵌套配置模型。

pydantic-settings 嵌套 ``BaseSettings`` 不继承父级 ``env_file``，默认只读取
OS 环境变量；若父级不显式向下传递 env_file，``.env.local`` 中的
``RATE_LIMIT_*``/``MODEL_GATEWAY_*``/``DOCUMENT_*``/``RETRIEVAL_*`` 会被静默
忽略——应用按设计拒绝启动（startup configuration failed），用户从文件补全
变量也无效。本测试锁定嵌套配置必须从父级解析的环境文件取值。
"""

from pathlib import Path

import app.core.settings as settings_module
from app.core.settings import Settings


def test_nested_settings_read_resolved_env_file(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "APP_ENV=development",
                "RATE_LIMIT_SUBJECT_HMAC_KEY=nested-rate-secret",
                "MODEL_GATEWAY_ENDPOINT=http://127.0.0.1:18000/v1",
                "MODEL_GATEWAY_API_KEY=nested-gateway-key",
                "MODEL_GATEWAY_QUERY_REWRITE_MODEL=nested-rewrite",
                "MODEL_GATEWAY_GENERATION_MODEL=nested-gen",
                "DOCUMENT_STORAGE_ROOT=" + str(tmp_path / "store"),
                "RETRIEVAL_VECTOR_MIN_SIMILARITY=0.80",
                "MESSAGE_STREAMING_STALE_SECONDS=480",
            ]
        ),
        encoding="utf-8",
    )
    # 父级 Settings 与嵌套模型解析同一环境文件（生产装配方式：Settings() 由
    # model_config 解析，嵌套模型由 default_factory 传入同一文件）。
    monkeypatch.setattr(settings_module, "_resolve_env_file", lambda: str(env_file))
    # conftest 会把同名变量 setdefault 进 OS 环境且优先级高于 dotenv 文件，
    # 必须清除，才能证明取值来自环境文件而非环境变量。
    for var in (
        "APP_ENV",
        "AUTH_JWT_SECRET_KEY",
        "RATE_LIMIT_SUBJECT_HMAC_KEY",
        "MODEL_GATEWAY_ENDPOINT",
        "MODEL_GATEWAY_API_KEY",
        "MODEL_GATEWAY_QUERY_REWRITE_MODEL",
        "MODEL_GATEWAY_GENERATION_MODEL",
        "DOCUMENT_STORAGE_ROOT",
        "RETRIEVAL_VECTOR_MIN_SIMILARITY",
        "MESSAGE_STREAMING_STALE_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings()

    assert settings.rate_limit.subject_hmac_key.get_secret_value() == "nested-rate-secret"
    assert settings.model_gateway.endpoint == "http://127.0.0.1:18000/v1"
    assert settings.model_gateway.generation_model == "nested-gen"
    assert settings.storage.storage_root == str(tmp_path / "store")
    assert settings.retrieval.vector_min_similarity == 0.80
    assert settings.retrieval.message_streaming_stale_seconds == 480
