"""Celery 任务传输边界回归（T163 / FR-008b）。"""

import importlib
import uuid
from collections.abc import Callable

import pytest

from app.workers import base
from app.workers.celery_app import celery_app

pytestmark = pytest.mark.unit


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_dispatch_task_serializes_uuid_before_cross_process_transport(monkeypatch) -> None:
    """领域 UUID 不得直接进入 Celery broker 载荷。"""
    task_id = uuid.uuid4()
    captured: dict[str, object] = {}

    def send_task(name: str, *, args: tuple[object, ...]) -> None:
        captured["name"] = name
        captured["args"] = args

    monkeypatch.setattr(celery_app, "send_task", send_task)

    base.dispatch_task("orionamesh.document_parse", (task_id,))

    assert captured == {
        "name": "orionamesh.document_parse",
        "args": (str(task_id),),
    }


@pytest.mark.parametrize("wire_task_id", [str(uuid.uuid4()), uuid.uuid4()])
def test_execute_document_task_normalizes_string_and_legacy_uuid_payloads(
    monkeypatch, wire_task_id: str | uuid.UUID
) -> None:
    """worker 入口只规范化一次，再以 UUID 调用持久化处理逻辑。"""
    expected_task_id = uuid.UUID(str(wire_task_id))
    session = _Session()
    boundaries = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 1)
    received: dict[str, object] = {}

    monkeypatch.setattr("app.infrastructure.database.session.SessionLocal", lambda: session)
    monkeypatch.setattr(base, "load_task_boundaries", lambda _session, task_id: boundaries)

    def process(_session, **kwargs: object) -> None:
        received.update(kwargs)

    base.execute_document_task(wire_task_id, process)

    assert received == {
        "task_id": expected_task_id,
        "user_id": boundaries[0],
        "knowledge_base_id": boundaries[1],
        "document_id": boundaries[2],
        "document_version": boundaries[3],
    }
    assert session.closed is True


@pytest.mark.parametrize(
    ("module_name", "task_name"),
    [
        ("document_parse", "orionamesh.document_parse"),
        ("document_chunk", "orionamesh.document_chunk"),
        ("document_embed", "orionamesh.document_embed"),
        ("document_finalize", "orionamesh.document_finalize"),
        ("document_cleanup", "orionamesh.document_cleanup"),
        ("document_delete_cleanup", "orionamesh.document_delete_cleanup"),
    ],
)
def test_every_registered_document_worker_uses_shared_transport_boundary(
    monkeypatch, module_name: str, task_name: str
) -> None:
    """六类资料任务不能各自重新解析跨进程 task_id。"""
    module = importlib.import_module(f"app.workers.{module_name}")
    task_id = uuid.uuid4()
    captured: list[tuple[object, Callable[..., object]]] = []

    monkeypatch.setattr(
        module,
        "execute_document_task",
        lambda received_task_id, process: captured.append((received_task_id, process)),
    )

    celery_app.tasks[task_name].run(task_id)

    assert captured and captured[0][0] == task_id
