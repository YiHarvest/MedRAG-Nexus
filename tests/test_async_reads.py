"""验证异步读取任务的执行与结果持久化。"""

from __future__ import annotations

from types import SimpleNamespace

from jd_knowledge.core.ids import new_task_id
from jd_knowledge.core.models import TaskRecord, TaskStatus, WorkspaceListResponse
from jd_knowledge.services.processing import process_task


class CapturingTaskLog:
    async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
        return None


async def test_worker_executes_list_workspaces_and_stores_result() -> None:
    task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="-",
        workspace_name="-",
        operation="list_workspaces",
    )

    class FakeMetadata:
        def __init__(self) -> None:
            self.updates: list[dict[str, object]] = []

        async def get_task(self, task_id: str):
            return task

        async def update_task(self, task_id: str, **fields: object) -> None:
            self.updates.append(fields)

        async def list_workspaces(self, user_id: str):
            return WorkspaceListResponse(user_id=user_id, workspaces=[])

    runtime = SimpleNamespace(metadata=FakeMetadata(), task_log=CapturingTaskLog())
    await process_task(runtime, task.task_id)

    completed = next(update for update in runtime.metadata.updates if update.get("status") == TaskStatus.SUCCEEDED)
    assert completed["result"] == {"user_id": "user-001", "workspaces": []}
    assert completed["progress"].percent == 100
