"""Celery 应用（T050 / data-model.md 任务真相源边界）。

- Redis/Celery 仅执行或传输；任务状态真相只以 PostgreSQL 为准；
- 提交后投递：worker 自行在事务内锁定任务并复查 ``queued``（重复投递幂等）；
- 维护扫描器（上传批次接管、处理名额回收、过期幂等清理、queued 重投、
  streaming 消息收敛）由 Celery Beat 周期触发。
"""

from celery import Celery

from app.core.redis import TASK_QUEUE_NAME, broker_url

celery_app = Celery("orionamesh", broker=broker_url())

celery_app.conf.task_default_queue = TASK_QUEUE_NAME
# 手动确认：worker 崩溃后任务回到队列，配合任务级重试预算收敛。
celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1
celery_app.conf.task_track_started = True
celery_app.conf.broker_connection_retry_on_startup = True

celery_app.conf.beat_schedule = {
    "orionamesh-maintenance-scan": {
        "task": "orionamesh.maintenance_scan",
        "schedule": 30.0,
    }
}

# 显式注册阶段 worker 任务（提交后投递适配层）。
from app.workers import (  # noqa: E402
    document_chunk,
    document_cleanup,
    document_delete_cleanup,
    document_embed,
    document_finalize,
    document_parse,
)

for _module in (
    document_parse,
    document_chunk,
    document_embed,
    document_finalize,
    document_cleanup,
    document_delete_cleanup,
):
    _module.register_tasks(celery_app)


@celery_app.task(name="orionamesh.maintenance_scan")
def maintenance_scan_task() -> None:
    """维护扫描器入口（Beat 触发）；各扫描在独立事务中收敛。"""
    from app.core.settings import get_settings
    from app.infrastructure.database.session import SessionLocal
    from app.services.file_storage import default_file_storage
    from app.workers.task_recovery import run_maintenance_scan

    settings = get_settings()
    session = SessionLocal()
    try:
        run_maintenance_scan(
            session, storage=default_file_storage(), dispatch=dispatch_task_name, settings=settings
        )
    finally:
        session.close()


def dispatch_task_name(name: str, args: tuple) -> None:
    """Beat 扫描内部投递入口（与 worker 投递同一路径）。"""
    from app.workers.base import dispatch_task

    dispatch_task(name, args)
