"""验证缺失制品和失败摄取场景下的删除行为。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from medrag_nexus.core.ids import file_id, new_task_id
from medrag_nexus.core.models import DeleteFileRequest, TaskRecord, TaskStatus, WorkspaceRecord
from medrag_nexus.services.files import FileService
from medrag_nexus.services.processing import process_delete


class CapturingTaskLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
        self.events.append((level, message, {"task_id": task_id, **context}))


class FakeTasks:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.released: list[str] = []

    async def health(self) -> None:
        return None

    @asynccontextmanager
    async def workspace_lock(self, user_id: str, workspace_id: str, *, wait_seconds=None):
        yield

    async def reserve_file(self, user_id: str, workspace_id: str, selected_file_id: str, task_id: str) -> None:
        return None

    async def release_file(self, user_id: str, workspace_id: str, selected_file_id: str, task_id: str) -> None:
        self.released.append(selected_file_id)

    async def enqueue(self, task_id: str) -> None:
        self.enqueued.append(task_id)


async def test_submit_delete_accepts_file_already_removed_by_failed_ingestion() -> None:
    selected_file_id = file_id()
    workspace = WorkspaceRecord(
        workspace_id="workspace_11111111-1111-5111-8111-111111111111",
        user_id="user-001",
        workspace_name="测试知识库",
    )

    class FakeMetadata:
        def __init__(self) -> None:
            self.created: list[TaskRecord] = []

        async def workspace_repair_reason(self, workspace_id: str):
            return None

        async def get_workspace(self, workspace_id: str):
            return workspace

        async def get_file(self, workspace_id: str, requested_file_id: str):
            assert requested_file_id == selected_file_id
            return None

        async def create_task(self, task: TaskRecord) -> None:
            self.created.append(task)

    runtime = SimpleNamespace(metadata=FakeMetadata(), tasks=FakeTasks(), task_log=CapturingTaskLog())
    response = await FileService(runtime).submit_delete(
        DeleteFileRequest(
            user_id=workspace.user_id,
            workspace_id=workspace.workspace_id,
            file_id=selected_file_id,
            file_name="failed.pdf",
        )
    )

    assert response.status == "queued"
    assert runtime.tasks.enqueued == [response.task_id]
    assert runtime.metadata.created[0].payload["allow_missing"] is True


async def test_delete_worker_completes_when_failed_ingestion_file_is_already_absent() -> None:
    selected_file_id = file_id()
    task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="workspace_11111111-1111-5111-8111-111111111111",
        workspace_name="测试知识库",
        operation="delete_file",
        payload={"file_id": selected_file_id, "file_name": "failed.pdf", "allow_missing": True},
    )

    class FakeMetadata:
        def __init__(self) -> None:
            self.updates: list[dict[str, object]] = []

        async def get_task(self, task_id: str):
            return task

        async def update_task(self, task_id: str, **fields: object) -> None:
            self.updates.append(fields)

        async def get_file(self, workspace_id: str, requested_file_id: str):
            return None

    class FakeArtifacts:
        def __init__(self) -> None:
            self.cleaned: list[str] = []

        async def cleanup_staging(self, task_id: str) -> None:
            self.cleaned.append(task_id)

    runtime = SimpleNamespace(
        metadata=FakeMetadata(),
        tasks=FakeTasks(),
        task_log=CapturingTaskLog(),
        artifacts=FakeArtifacts(),
    )

    await process_delete(runtime, task.task_id)

    completed = next(update for update in runtime.metadata.updates if update.get("status") == TaskStatus.SUCCEEDED)
    assert completed["result"]["already_absent"] is True
    assert completed["result"]["deleted"] is True
    assert runtime.tasks.released == [selected_file_id]
    assert runtime.artifacts.cleaned == [task.task_id]
