"""验证异步任务回调的载荷与投递行为。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from jd_knowledge.core.ids import new_task_id
from jd_knowledge.core.models import TaskProgress, TaskRecord, TaskStatus
from jd_knowledge.services import callbacks


class CapturingTaskLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
        self.events.append((level, message, {"task_id": task_id, **context}))


async def test_callback_posts_task_id_status_progress_and_result(monkeypatch) -> None:
    requests: list[tuple[str, dict, dict]] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url: str, *, json: dict, headers: dict):
            requests.append((url, json, headers))
            return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(callbacks.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    runtime = SimpleNamespace(task_log=CapturingTaskLog())
    payload = {
        "event": "task.succeeded",
        "task_id": "a" * 32,
        "status": "succeeded",
        "stage": "completed",
        "progress": {"current": 100, "total": 100, "percent": 100.0},
        "result": {"count": 2},
    }

    await callbacks.deliver_task_callback(runtime, "https://callback.example/hook?token=secret", payload)

    assert requests[0][1]["task_id"] == "a" * 32
    assert requests[0][1]["progress"]["percent"] == 100.0
    assert requests[0][1]["result"] == {"count": 2}
    assert requests[0][2]["X-JD-Knowledge-Task-ID"] == "a" * 32
    assert runtime.task_log.events[0][2]["callback_url"] == "https://callback.example/hook"


async def test_callback_snapshot_contains_generated_task_id_and_running_progress(monkeypatch) -> None:
    delivered: list[dict] = []

    async def fake_deliver(runtime, callback_url: str, payload: dict) -> None:
        delivered.append(payload)

    monkeypatch.setattr(callbacks, "deliver_task_callback", fake_deliver)
    task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="workspace_1",
        workspace_name="Knowledge",
        operation="retrieval",
        status=TaskStatus.RUNNING,
        stage="retrieving",
        progress=TaskProgress(current=50, total=100, percent=50),
        payload={"callback_url": "https://callback.example/hook"},
    )

    callbacks.schedule_task_callback(SimpleNamespace(), task)
    await asyncio.sleep(0)

    assert delivered[0]["task_id"] == task.task_id
    assert delivered[0]["status"] == "running"
    assert delivered[0]["progress"] == {"current": 50, "total": 100, "percent": 50.0}
