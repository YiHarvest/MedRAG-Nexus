"""验证 SQLite 权威状态、迁移和事务可见性。"""

from __future__ import annotations

import sqlite3

from jd_knowledge.core.ids import file_id, new_id, new_task_id
from jd_knowledge.core.models import ResourceRecord, TaskProgress, TaskRecord, TaskStatus, WorkspaceRecord, local_now
from jd_knowledge.storage.sqlite import SQLiteStore


async def test_list_users_returns_sorted_sqlite_users_and_basic_stats(tmp_path) -> None:
    path = tmp_path / "metadata.sqlite3"
    store = SQLiteStore(path)
    await store.ensure()

    with sqlite3.connect(path) as db:
        db.executemany(
            """
            INSERT INTO users(
                user_id, user_name, resource_count, file_count, str_count, total_size_bytes, created_at, modified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("user-b", "用户 B", 3, 2, 1, 2048, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                ("user-a", "用户 A", 0, 0, 0, 0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ],
        )
        db.executemany(
            """
            INSERT INTO workspaces(
                workspace_id, user_id, workspace_name, created_at, modified_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("workspace-b1", "user-b", "B1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                ("workspace-b2", "user-b", "B2", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ],
        )

    response = await store.list_users()

    assert [item.user_id for item in response.users] == ["user-a", "user-b"]
    assert response.users[0].model_dump() == {
        "user_id": "user-a",
        "user_name": "用户 A",
        "workspace_count": 0,
        "resource_count": 0,
        "file_count": 0,
        "str_count": 0,
        "total_size_bytes": 0,
    }
    assert response.users[1].model_dump() == {
        "user_id": "user-b",
        "user_name": "用户 B",
        "workspace_count": 2,
        "resource_count": 3,
        "file_count": 2,
        "str_count": 1,
        "total_size_bytes": 2048,
    }


async def test_create_user_persists_empty_user_and_rejects_duplicate_id(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "metadata.sqlite3")
    await store.ensure()

    created = await store.create_user("user_generated", "测试用户")
    duplicate = await store.create_user("user_generated", "另一个名称")

    assert created is not None
    assert created.model_dump() == {
        "user_id": "user_generated",
        "user_name": "测试用户",
        "workspace_count": 0,
        "resource_count": 0,
        "file_count": 0,
        "str_count": 0,
        "total_size_bytes": 0,
    }
    assert duplicate is None
    assert (await store.list_users()).users == [created]


async def test_workspace_lifecycle_updates_denormalized_rows_and_user_totals(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "metadata.sqlite3")
    await store.ensure()
    workspace = WorkspaceRecord(
        workspace_id="workspace_lifecycle",
        user_id="user-owner",
        workspace_name="Original",
    )
    await store.create_workspace(workspace)
    renamed = await store.rename_workspace(workspace.workspace_id, "Renamed")

    assert renamed.workspace_name == "Renamed"
    assert (await store.list_workspaces(workspace.user_id)).workspaces[0].workspace_name == "Renamed"
    assert not await store.workspace_has_active_tasks(workspace.workspace_id)

    deleted = await store.delete_workspace(workspace.workspace_id)
    assert deleted.workspace_name == "Renamed"
    assert await store.get_workspace(workspace.workspace_id) is None
    assert (await store.list_workspaces(workspace.user_id)).workspaces == []


async def test_sqlite_is_authority_for_tasks_resources_and_stats(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "metadata.sqlite3")
    await store.ensure()

    user_id = "user-001"
    workspace_name = "产品知识库"
    current_workspace_id = "workspace_11111111-1111-5111-8111-111111111111"
    task = TaskRecord(
        task_id=new_task_id(),
        user_id=user_id,
        workspace_id=current_workspace_id,
        workspace_name=workspace_name,
        operation="add_file",
    )
    await store.create_task(task)
    assert (await store.get_task(task.task_id)).task_id == task.task_id  # type: ignore[union-attr]

    now = local_now()
    resource = ResourceRecord(
        document_id=new_id(),
        workspace_id=current_workspace_id,
        user_id=user_id,
        workspace_name=workspace_name,
        source_type="file",
        file_id=file_id(),
        file_name="报告.pdf",
        mime_type="application/pdf",
        content_hash="sha256:" + "a" * 32,
        size_bytes=1536,
        markdown_hash="sha256:" + "b" * 32,
        parser="pdf",
        chunk_count=2,
        artifact_path="/tmp/report",
        created_at=now,
        modified_at=now,
    )
    await store.add_resource(resource, lambda: None)

    workspaces = await store.list_workspaces(user_id)
    assert workspaces.workspaces[0].workspace_id == current_workspace_id
    assert workspaces.workspaces[0].file_count == 1
    assert workspaces.workspaces[0].total_size_bytes == 1536
    files, strings, stats = await store.list_resources(current_workspace_id)
    assert stats.total_size_bytes == 1536
    assert files[0].size_bytes == 1536
    assert strings == []


async def test_sqlite_migrates_existing_task_operation_constraint_for_async_reads(tmp_path) -> None:
    path = tmp_path / "metadata.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                workspace_name TEXT NOT NULL,
                operation TEXT NOT NULL CHECK(operation IN ('add_file', 'add_str', 'delete_file')),
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                journal_json TEXT NOT NULL,
                result_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                modified_at TEXT NOT NULL
            );
            CREATE INDEX tasks_status_idx ON tasks(status, modified_at);
            """
        )

    store = SQLiteStore(path)
    await store.ensure()
    task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="-",
        workspace_name="-",
        operation="list_workspaces",
    )
    await store.create_task(task)

    assert (await store.get_task(task.task_id)).operation == "list_workspaces"  # type: ignore[union-attr]

    delete_task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="workspace_1",
        workspace_name="Knowledge",
        operation="delete_string",
        payload={"content_hash": "sha256:" + "a" * 32},
    )
    await store.create_task(delete_task)
    assert (await store.get_task(delete_task.task_id)).operation == "delete_string"  # type: ignore[union-attr]


async def test_resource_visibility_and_task_success_commit_atomically(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "metadata.sqlite3")
    await store.ensure()
    now = local_now()
    task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id="workspace_1",
        workspace_name="Knowledge",
        operation="add_file",
        status="running",
        started_at=now,
    )
    await store.create_task(task)
    resource = ResourceRecord(
        document_id=new_id(),
        workspace_id=task.workspace_id,
        user_id=task.user_id,
        workspace_name=task.workspace_name,
        source_type="file",
        file_id=file_id(),
        file_name="complete.txt",
        mime_type="text/plain",
        content_hash="sha256:" + "c" * 32,
        size_bytes=8,
        markdown_hash="sha256:" + "d" * 32,
        parser="text",
        chunk_count=1,
        artifact_path="/data/complete",
        created_at=now,
        modified_at=now,
    )
    task.payload["document_id"] = str(resource.document_id)
    await store.update_task(task.task_id, payload=task.payload)
    result = {"file_id": resource.file_id, "file_name": resource.file_name}

    await store.add_resource_and_complete_task(resource, task, result, now)

    stored_task = await store.get_task(task.task_id)
    records = await store.list_resource_records(task.workspace_id)
    assert stored_task is not None
    assert stored_task.status.value == "succeeded"
    assert stored_task.progress.percent == 100
    assert stored_task.result == result
    assert records[0].document_id == resource.document_id


async def test_existing_resources_are_migrated_from_task_success_state(tmp_path) -> None:
    path = tmp_path / "metadata.sqlite3"
    store = SQLiteStore(path)
    await store.ensure()
    now = local_now()
    workspace_id = "workspace_1"

    def make_resource(name: str) -> ResourceRecord:
        return ResourceRecord(
            document_id=new_id(),
            workspace_id=workspace_id,
            user_id="user-001",
            workspace_name="Knowledge",
            source_type="file",
            file_id=file_id(),
            file_name=name,
            mime_type="text/plain",
            content_hash="sha256:" + name.encode().hex().ljust(32, "0")[:32],
            size_bytes=8,
            markdown_hash="sha256:" + "e" * 32,
            parser="text",
            chunk_count=1,
            artifact_path=f"/data/{name}",
            created_at=now,
            modified_at=now,
        )

    complete = make_resource("complete.txt")
    failed = make_resource("failed.txt")
    complete_task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id=workspace_id,
        workspace_name="Knowledge",
        operation="add_file",
        status=TaskStatus.SUCCEEDED,
        stage="completed",
        progress=TaskProgress(current=100, total=100, percent=100),
        payload={"document_id": str(complete.document_id)},
    )
    failed_task = TaskRecord(
        task_id=new_task_id(),
        user_id="user-001",
        workspace_id=workspace_id,
        workspace_name="Knowledge",
        operation="add_file",
        status=TaskStatus.FAILED,
        stage="indexing",
        progress=TaskProgress(current=80, total=100, percent=80),
        payload={"document_id": str(failed.document_id)},
    )
    await store.create_task(complete_task)
    await store.create_task(failed_task)
    await store.add_resource(complete, lambda: None)
    await store.add_resource(failed, lambda: None)

    with sqlite3.connect(path) as db:
        db.executescript(
            """
            DROP INDEX resources_workspace_idx;
            ALTER TABLE resources RENAME TO resources_with_completion;
            CREATE TABLE resources (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL UNIQUE,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                user_id TEXT NOT NULL,
                workspace_name TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                file_id TEXT UNIQUE,
                name TEXT,
                mime_type TEXT,
                content_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                markdown_hash TEXT,
                parser TEXT NOT NULL,
                degraded INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL,
                artifact_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                modified_at TEXT NOT NULL
            );
            INSERT INTO resources(
                row_id, document_id, workspace_id, user_id, workspace_name, resource_type,
                file_id, name, mime_type, content_hash, size_bytes, markdown_hash, parser,
                degraded, chunk_count, artifact_path, created_at, modified_at
            ) SELECT
                row_id, document_id, workspace_id, user_id, workspace_name, resource_type,
                file_id, name, mime_type, content_hash, size_bytes, markdown_hash, parser,
                degraded, chunk_count, artifact_path, created_at, modified_at
            FROM resources_with_completion;
            DROP TABLE resources_with_completion;
            CREATE INDEX resources_workspace_idx ON resources(workspace_id, created_at, row_id);
            """
        )

    await store.ensure()

    visible = await store.list_resource_records(workspace_id)
    incomplete = await store.incomplete_resource_records(workspace_id)
    files, strings, stats = await store.list_resources(workspace_id)
    assert [resource.document_id for resource in visible] == [complete.document_id]
    assert [resource.document_id for resource in incomplete] == [failed.document_id]
    assert [item.file_name for item in files] == [complete.file_name]
    assert strings == []
    assert stats.resource_count == 1
    assert stats.file_count == 1
    assert stats.total_size_bytes == complete.size_bytes
