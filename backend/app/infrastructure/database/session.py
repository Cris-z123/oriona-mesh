"""SQLAlchemy 引擎与会话。

连接串只来自唯一根配置模块（``app.core.settings``）；引擎惰性建连，
进程启动时不依赖数据库在线。同步驱动 psycopg3 同时服务于 API 线程池与 Celery worker。
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url_value,
    pool_pre_ping=True,
    # 数据库不可达时就绪检查/健康检查快速失败，避免连接无限挂起。
    connect_args={"connect_timeout": 3},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话，请求结束统一关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
