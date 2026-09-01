"""验证入库任务可从排队和运行状态安全取消。"""

from __future__ import annotations

from jd_knowledge.core.models import TaskRecord, TaskStatus
from jd_knowledge.services.files import FileService


class FakeMetadata:
    def __init__(self, task: TaskRecord) -> None:
        self.task = task

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return self.task if task_id == self.task.task_id else None

    async def update_task(self, task_id: str, **fields: object) -> None:
        assert task_id == self.task.task_id
        for key, value in fields.items():
            setattr(self.task, key, value)

    async def block_workspace(self, workspace_id: str, reason: str) -> None:  # pragma: no cover
        raise AssertionError(f"unexpected repair block: {workspace_id} {reason}")


class FakeTasks:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def cancel_queued(self, task_id: str) -> bool:
        return True

    async def release_content(
        self,
        user_id: str,
        workspace_id: str,
        source_type: str,
        digest: str,
        task_id: str,
    ) -> None:
        self.released.append(task_id)


class FakeArtifacts:
    def __init__(self) -> None:
        self.cleaned: list[str] = []

    async def cleanup_staging(self, task_id: str) -> None:
        self.cleaned.append(task_id)


class FakeTaskLog:
    async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
        return None


async def test_cancel_queued_ingestion_marks_task_cancelled_and_cleans_staging() -> None:
    task = TaskRecord(
        task_id="a" * 32,
        user_id="user-001",
        workspace_id="workspace_11111111-1111-5111-8111-111111111111",
        workspace_name="测试知识库",
        operation="add_file",
        payload={"source_type": "file", "content_hash": "sha256:test"},
    )

    class FakeRuntime:
        def __init__(self) -> None:
            self.metadata = FakeMetadata(task)
            self.tasks = FakeTasks()
            self.artifacts = FakeArtifacts()
            self.task_log = FakeTaskLog()

        async def cancel_active_task(self, task_id: str) -> bool:
            return False

    runtime = FakeRuntime()
    response = await FileService(runtime).cancel_ingestion(task.task_id, task.user_id)  # type: ignore[arg-type]

    assert response.status == TaskStatus.FAILED
    assert response.stage == "cancelled"
    assert response.error is not None
    assert response.error.code == "TASK_CANCELLED"
    assert runtime.artifacts.cleaned == [task.task_id]
    assert runtime.tasks.released == [task.task_id]
