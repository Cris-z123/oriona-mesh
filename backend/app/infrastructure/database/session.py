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
    settings.database_url,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话，请求结束统一关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
