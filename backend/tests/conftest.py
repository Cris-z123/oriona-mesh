"""pytest 共享夹具与环境。

测试发现规则见 ``backend/pyproject.toml`` 的 ``[tool.pytest.ini_options]``：
``tests/`` 下按 ``unit/``、``integration/``、``contract/``、``architecture/``、``security/``
分目录组织，通过已注册标记区分；``pythonpath=[\".\"]`` 使 ``app`` 包可直接导入。

环境约定：自动化测试加载 ``.env.test``（如存在）并设置 ``APP_ENV=test``，随后在
导入 ``app.main`` 之前设置标准测试环境变量（setdefault，不覆盖外部提供值）。
集成测试使用 ``TEST_DATABASE_URL``（未设置时回退 ``DATABASE_URL``）；Redis 使用
``REDIS_URL`` 配置（默认 localhost:6379）。数据库与 Redis 不可用时相关测试显式
skip，不静默失败。
"""

import os
import tempfile
import uuid
from collections.abc import Generator
from pathlib import Path

# 先加载 .env.test（python-dotenv 不覆盖已存在的环境变量），再固定测试环境模式。
from dotenv import load_dotenv

load_dotenv(".env.test", override=False)
os.environ.setdefault("APP_ENV", "test")
# 测试共享持久卷根目录：系统临时目录，避免客户端测试写入真实 /data/orionamesh。
os.environ.setdefault(
    "DOCUMENT_STORAGE_ROOT",
    str(Path(tempfile.gettempdir()) / "orionamesh-test-storage"),
)
os.environ.setdefault("AUTH_JWT_SECRET_KEY", "test-jwt-secret-" + "x" * 32)
os.environ.setdefault("RATE_LIMIT_SUBJECT_HMAC_KEY", "test-rate-limit-" + "y" * 32)
os.environ.setdefault(
    "MODEL_GATEWAY_ENDPOINT",
    "http://localhost:19999/v1",  # 本机回环例外，仅测试
)
os.environ.setdefault("MODEL_GATEWAY_API_KEY", "test-gateway-key")
os.environ.setdefault("MODEL_GATEWAY_QUERY_REWRITE_MODEL", "test-rewrite")
os.environ.setdefault("MODEL_GATEWAY_GENERATION_MODEL", "test-gen")
# 提供 TEST_DATABASE_URL 时，应用（DATABASE_URL）与测试夹具统一使用测试库，
# 避免契约测试写入开发数据库；未提供时回退本地开发默认值。
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL")
    or "postgresql+psycopg://orionamesh:orionamesh@localhost:5432/orionamesh",
)
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

# 环境装配必须在导入应用之前完成，故导入块整体豁免 E402。
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

# 注册全部表到 Base.metadata（create_all/drop_all 使用）。
import app.models  # noqa: F401, E402
from app.db.base import Base  # noqa: E402
from app.infrastructure.storage.local import LocalStorage  # noqa: E402
from app.services.file_storage import FileStorage  # noqa: E402


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """真实应用（含 trace、限流中间件与统一错误信封处理器）的同步测试客户端。

    ``raise_server_exceptions=False``：未处理异常由 500 处理器像生产环境一样接管
    并返回统一信封（Starlette 1.6 的 ServerErrorMiddleware 发送 500 后总是重抛，
    默认配置会让测试客户端直接抛出异常而非断言响应）。
    """
    with TestClient(fastapi_app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def test_engine():
    """会话级测试数据库引擎：确保扩展、清空并重建全部表；不可用时 skip。

    测试库不存在时尝试自动创建（连接同服务器维护库执行 CREATE DATABASE）；
    无权限或不可达时 skip 并给出清晰原因。
    """
    from sqlalchemy.engine import make_url

    from app.core.settings import get_settings

    url = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url_value
    parsed = make_url(url)
    try:
        _ensure_test_database(parsed)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.skip(f"test database unavailable: {exc}")
    # 彻底重置：DROP SCHEMA CASCADE 同时清除枚举类型与扩展（drop_all 不删原生
    # enum，残留类型会导致重建时 "invalid input value for enum"）。
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    # 扩展必须在 schema 重置后重新安装（vector/pg_trgm/pgcrypto 随 schema 被清除）。
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            conn.commit()
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.skip(f"required extensions unavailable: {exc}")
    Base.metadata.create_all(engine)
    yield engine
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    engine.dispose()


def _ensure_test_database(parsed) -> None:
    """测试库缺失时自动创建；无权限/不可达时抛出让夹具 skip。"""
    from sqlalchemy import create_engine as _create_engine

    if parsed.database == "postgres":
        return  # 显式使用维护库：不自动创建
    maintenance = parsed.set(database="postgres")
    admin = _create_engine(
        maintenance.render_as_string(hide_password=False),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )
    try:
        with admin.connect() as conn:
            # CREATE DATABASE 不能在事务块内执行：切 AUTOCOMMIT 使语句逐条提交。
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": parsed.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    except SQLAlchemyError as exc:
        raise RuntimeError(f"cannot ensure test database {parsed.database!r}: {exc}") from exc
    finally:
        admin.dispose()


@pytest.fixture
def db_session(test_engine) -> Generator[Session, None, None]:
    """每测试独立事务会话；结束后回滚并清空全部表，保证测试隔离。"""
    factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        _truncate_all(test_engine)


def _truncate_all(engine) -> None:
    with engine.connect() as conn:
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        if tables:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            conn.commit()


@pytest.fixture
def storage(tmp_path) -> FileStorage:
    """每测试独立持久卷（临时目录）；集成测试共用，避免各文件重复定义。"""
    return FileStorage(LocalStorage(tmp_path / "store"))


@pytest.fixture
def dispatch_calls():
    """记录投递调用的假 dispatch：(name, args)。"""
    calls: list[tuple[str, tuple]] = []

    def fake(name: str, args: tuple) -> None:
        calls.append((name, args))

    return fake, calls


@pytest.fixture
def user_and_kb(db_session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """直接创建用户与知识库（跳过 API），供服务层集成测试使用。"""
    from app.models.knowledge_base import KnowledgeBase
    from app.models.user import User

    user = User(email="shared-owner@example.com", password_hash="x" * 60)
    db_session.add(user)
    db_session.flush()
    kb = KnowledgeBase(user_id=user.id, name="kb")
    db_session.add(kb)
    db_session.commit()
    return user.id, kb.id


@pytest.fixture(scope="module")
def redis_client():
    """模块级真实 Redis 客户端；不可用时 skip，并在结束时清理测试键。"""
    from app.core.redis import get_redis_client

    client = get_redis_client()
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001 - RedisError 家族
        pytest.skip(f"redis unavailable: {exc}")
    yield client
    for key in client.scan_iter("rl:*"):
        client.delete(key)


@pytest.fixture
def clean_rate_limit_keys(redis_client):
    """每测试清空限流键，避免 IP/账号预算跨测试泄漏（需 Redis 的模块显式声明）。"""
    yield
    for key in redis_client.scan_iter("rl:*"):
        redis_client.delete(key)


# 延迟导入：先完成环境变量装配再导入应用。
from app.main import app as fastapi_app  # noqa: E402, F401
