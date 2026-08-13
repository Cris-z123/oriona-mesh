"""SQLAlchemy 声明基类。

所有 ORM 模型（Phase 2 的 ``app/models/``）继承此 Base；Alembic 的 ``target_metadata``
与测试共用它，保证自动生成迁移与物理建表使用同一元数据。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """统一 ORM 基类；供 ORM 模型、Alembic 与测试共用同一元数据。"""

    pass
