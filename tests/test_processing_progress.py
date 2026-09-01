"""验证大文件处理进度、并发限制与资源可见性。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from medrag_nexus.core.ids import file_id, new_id, new_task_id
from medrag_nexus.core.models import ResourceRecord, TaskRecord, local_now
from medrag_nexus.pipeline.parsers import ParseResult
from medrag_nexus.services import processing


async def test_large_file_parsing_keeps_advancing_before_mineru_returns(monkeypatch, tmp_path) -> None:
    progress_values: list[int] = []

    class FakeMetadata:
        async def update_task(self, task_id: str, **fields: object) -> None:
            progress = fields.get("progress")
            if progress is not None:
                progress_values.append(progress.current)

    class FakeTaskLog:
        async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
            return None

    async def slow_parse(path: Path, settings: object, progress):
        await progress("INFO", "正在连接 MinerU", {})
        await asyncio.sleep(0.035)
        await progress("INFO", "MinerU 响应完成", {})
        return ParseResult(markdown="# parsed", parser="mineru")

    monkeypatch.setattr(processing, "parse_file", slow_parse)
    monkeypatch.setattr(processing, "_PARSING_PROGRESS_INTERVAL_SECONDS", 0.01)
    task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="workspace_1",
        workspace_name="Knowledge",
        operation="add_file",
    )
    runtime = SimpleNamespace(metadata=FakeMetadata(), task_log=FakeTaskLog(), settings=SimpleNamespace())

    result = await processing._parse_file_with_progress(runtime, task, tmp_path / "large.pdf", "large.pdf")

    assert result.markdown == "# parsed"
    assert progress_values[0] == 10
    assert 15 in progress_values
    assert any(15 < value < 20 for value in progress_values)
    assert progress_values[-1] >= 20
    assert progress_values == sorted(progress_values)
    assert max(progress_values) <= 44


async def test_large_file_tasks_wait_for_a_bounded_ingestion_slot(monkeypatch) -> None:
    stages: list[str] = []

    class FakeMetadata:
        async def update_task(self, task_id: str, **fields: object) -> None:
            stages.append(str(fields.get("stage")))

    class FakeTaskLog:
        async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
            return None

    task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="workspace_1",
        workspace_name="Knowledge",
        operation="add_file",
    )
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    runtime = SimpleNamespace(
        metadata=FakeMetadata(),
        task_log=FakeTaskLog(),
        settings=SimpleNamespace(file_ingestion_concurrency=1),
        file_ingestion_semaphore=semaphore,
    )
    monkeypatch.setattr(processing, "_FILE_SLOT_WAIT_LOG_INTERVAL_SECONDS", 0.01)

    waiting = asyncio.create_task(processing._acquire_file_ingestion_slot(runtime, task))
    await asyncio.sleep(0.025)
    assert not waiting.done()
    assert stages.count("waiting_for_parser") >= 2

    semaphore.release()
    await asyncio.wait_for(waiting, timeout=0.1)
    assert semaphore.locked()
    semaphore.release()


async def test_three_large_files_never_parse_above_configured_concurrency(monkeypatch) -> None:
    class FakeMetadata:
        async def update_task(self, task_id: str, **fields: object) -> None:
            return None

    class FakeTaskLog:
        async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
            return None

    runtime = SimpleNamespace(
        metadata=FakeMetadata(),
        task_log=FakeTaskLog(),
        settings=SimpleNamespace(file_ingestion_concurrency=1),
        file_ingestion_semaphore=asyncio.Semaphore(1),
    )
    monkeypatch.setattr(processing, "_FILE_SLOT_WAIT_LOG_INTERVAL_SECONDS", 0.01)
    active = 0
    maximum_active = 0
    completed: list[str] = []

    async def simulate_large_file(index: int) -> None:
        nonlocal active, maximum_active
        task = TaskRecord(
            task_id=new_task_id(),
            user_id="user-001",
            workspace_id="workspace_1",
            workspace_name="Knowledge",
            operation="add_file",
        )
        await processing._acquire_file_ingestion_slot(runtime, task)
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.015)
        active -= 1
        completed.append(str(index))
        runtime.file_ingestion_semaphore.release()

    await asyncio.gather(*(simulate_large_file(index) for index in range(3)))

    assert maximum_active == 1
    assert len(completed) == 3


async def test_resource_is_published_before_metadata_becomes_list_visible(tmp_path) -> None:
    events: list[str] = []

    class FakeMetadata:
        async def update_task(self, task_id: str, **fields: object) -> None:
            return None

        async def add_resource_and_complete_task(
            self,
            resource: ResourceRecord,
            task: TaskRecord,
            result: dict[str, object],
            finished_at,
        ) -> int:
            events.append("metadata")
            return 7

    class FakeArtifacts:
        @staticmethod
        def publish_file(prepared: Path, target: Path) -> None:
            events.append("artifact")

        async def cleanup_staging(self, task_id: str) -> None:
            return None

    class FakeTaskLog:
        async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
            return None

    now = local_now()
    task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="workspace_1",
        workspace_name="Knowledge",
        operation="add_file",
    )
    resource = ResourceRecord(
        document_id=new_id(),
        workspace_id=task.workspace_id,
        user_id=task.user_id,
        workspace_name=task.workspace_name,
        source_type="file",
        file_id=file_id(),
        file_name="report.pdf",
        mime_type="application/pdf",
        content_hash="sha256:" + "a" * 32,
        size_bytes=100,
        markdown_hash="sha256:" + "b" * 32,
        parser="mineru",
        chunk_count=1,
        artifact_path=str(tmp_path / "target"),
        created_at=now,
        modified_at=now,
    )
    runtime = SimpleNamespace(
        metadata=FakeMetadata(),
        artifacts=FakeArtifacts(),
        task_log=FakeTaskLog(),
    )

    await processing._publish_then_record_resource(
        runtime,
        task,
        resource,
        source_type="file",
        prepared=tmp_path / "prepared",
        target=tmp_path / "target",
        digest=resource.content_hash,
        result={"file_id": resource.file_id},
    )

    assert events == ["artifact", "metadata"]
    assert resource.row_id == 7
    assert task.journal["artifact_finalized"] is True
    assert task.journal["metadata_written"] is True
    assert task.status.value == "succeeded"
    assert task.progress.percent == 100
