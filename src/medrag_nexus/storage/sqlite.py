"""使用 SQLite 持久化任务、Workspace 和资源元数据。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from medrag_nexus.core.models import (
    FileListItem,
    ResourceRecord,
    StringListItem,
    TaskProgress,
    TaskRecord,
    TaskStatus,
    UserListItem,
    UserListResponse,
    WorkspaceListItem,
    WorkspaceListResponse,
    WorkspaceRecord,
    WorkspaceStats,
    local_now,
)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class SQLiteStore:
    """Authoritative AgentHub metadata and task store."""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    async def ensure(self) -> None:
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)

        def create() -> None:
            with self._connect() as db:
                db.execute("PRAGMA journal_mode = WAL")
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        user_name TEXT NOT NULL COLLATE BINARY,
                        resource_count INTEGER NOT NULL DEFAULT 0 CHECK(resource_count >= 0),
                        file_count INTEGER NOT NULL DEFAULT 0 CHECK(file_count >= 0),
                        str_count INTEGER NOT NULL DEFAULT 0 CHECK(str_count >= 0),
                        total_size_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_size_bytes >= 0),
                        created_at TEXT NOT NULL,
                        modified_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS workspaces (
                        workspace_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(user_id),
                        workspace_name TEXT NOT NULL COLLATE BINARY,
                        resource_count INTEGER NOT NULL DEFAULT 0 CHECK(resource_count >= 0),
                        file_count INTEGER NOT NULL DEFAULT 0 CHECK(file_count >= 0),
                        str_count INTEGER NOT NULL DEFAULT 0 CHECK(str_count >= 0),
                        total_size_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_size_bytes >= 0),
                        created_at TEXT NOT NULL,
                        modified_at TEXT NOT NULL,
                        UNIQUE(user_id, workspace_name)
                    );

                    CREATE TABLE IF NOT EXISTS resources (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        document_id TEXT NOT NULL UNIQUE,
                        workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                        user_id TEXT NOT NULL,
                        workspace_name TEXT NOT NULL,
                        resource_type TEXT NOT NULL CHECK(resource_type IN ('file', 'str')),
                        file_id TEXT UNIQUE,
                        name TEXT,
                        mime_type TEXT,
                        content_hash TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                        markdown_hash TEXT,
                        parser TEXT NOT NULL,
                        degraded INTEGER NOT NULL DEFAULT 0,
                        chunk_count INTEGER NOT NULL CHECK(chunk_count >= 0),
                        artifact_path TEXT NOT NULL,
                        source_task_id TEXT,
                        ingestion_complete INTEGER NOT NULL DEFAULT 0 CHECK(ingestion_complete IN (0, 1)),
                        created_at TEXT NOT NULL,
                        modified_at TEXT NOT NULL,
                        UNIQUE(workspace_id, resource_type, content_hash),
                        CHECK((resource_type = 'file' AND file_id IS NOT NULL AND name IS NOT NULL)
                           OR (resource_type = 'str' AND file_id IS NULL AND name IS NULL))
                    );
                    CREATE INDEX IF NOT EXISTS resources_workspace_idx
                        ON resources(workspace_id, created_at, row_id);

                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        workspace_name TEXT NOT NULL,
                        operation TEXT NOT NULL CHECK(operation IN (
                            'add_file', 'add_str', 'delete_file', 'delete_string',
                            'list_workspaces', 'list_files', 'retrieval'
                        )),
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
                    CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status, modified_at);

                    CREATE TABLE IF NOT EXISTS repair_blocks (
                        workspace_id TEXT PRIMARY KEY,
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
                if "user_name" not in user_columns:
                    db.execute(
                        "ALTER TABLE users ADD COLUMN user_name TEXT NOT NULL DEFAULT '' COLLATE BINARY"
                    )
                    db.execute("UPDATE users SET user_name = user_id WHERE user_name = ''")
                task_schema = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
                ).fetchone()[0]
                if "list_workspaces" not in task_schema or "delete_string" not in task_schema:
                    db.executescript(
                        """
                        BEGIN IMMEDIATE;
                        DROP INDEX IF EXISTS tasks_status_idx;
                        ALTER TABLE tasks RENAME TO tasks_before_async_reads;
                        CREATE TABLE tasks (
                            task_id TEXT PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            workspace_id TEXT NOT NULL,
                            workspace_name TEXT NOT NULL,
                            operation TEXT NOT NULL CHECK(operation IN (
                                'add_file', 'add_str', 'delete_file', 'delete_string',
                                'list_workspaces', 'list_files', 'retrieval'
                            )),
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
                        INSERT INTO tasks SELECT * FROM tasks_before_async_reads;
                        DROP TABLE tasks_before_async_reads;
                        CREATE INDEX tasks_status_idx ON tasks(status, modified_at);
                        COMMIT;
                        """
                    )
                resource_columns = {
                    row[1] for row in db.execute("PRAGMA table_info(resources)").fetchall()
                }
                if "source_task_id" not in resource_columns or "ingestion_complete" not in resource_columns:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        if "source_task_id" not in resource_columns:
                            db.execute("ALTER TABLE resources ADD COLUMN source_task_id TEXT")
                        if "ingestion_complete" not in resource_columns:
                            db.execute(
                                "ALTER TABLE resources ADD COLUMN ingestion_complete INTEGER NOT NULL DEFAULT 0 "
                                "CHECK(ingestion_complete IN (0, 1))"
                            )
                        tasks_by_document: dict[str, sqlite3.Row] = {}
                        task_rows = db.execute(
                            "SELECT task_id, status, progress_json, payload_json FROM tasks "
                            "WHERE operation IN ('add_file', 'add_str') ORDER BY created_at ASC"
                        ).fetchall()
                        for task_row in task_rows:
                            document_id = _loads(task_row["payload_json"], {}).get("document_id")
                            if document_id:
                                tasks_by_document[str(document_id)] = task_row
                        for resource_row in db.execute("SELECT row_id, document_id FROM resources").fetchall():
                            task_row = tasks_by_document.get(str(resource_row["document_id"]))
                            progress = _loads(task_row["progress_json"], {}) if task_row else {}
                            complete = bool(
                                task_row
                                and task_row["status"] == "succeeded"
                                and float(progress.get("percent", 0)) == 100
                            )
                            db.execute(
                                "UPDATE resources SET source_task_id = ?, ingestion_complete = ? WHERE row_id = ?",
                                (task_row["task_id"] if task_row else None, int(complete), resource_row["row_id"]),
                            )
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise

        await asyncio.to_thread(create)

    async def health(self) -> None:
        def check() -> None:
            with self._connect() as db:
                row = db.execute("PRAGMA quick_check").fetchone()
                if row is None or row[0] != "ok":
                    raise RuntimeError("SQLite quick_check failed")

        await asyncio.to_thread(check)

    async def has_marker(self, key: str) -> bool:
        def read() -> bool:
            with self._connect() as db:
                return db.execute("SELECT 1 FROM schema_meta WHERE key = ?", (key,)).fetchone() is not None

        return await asyncio.to_thread(read)

    async def set_marker(self, key: str, value: str = "complete") -> None:
        def write() -> None:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

        await asyncio.to_thread(write)

    async def create_task(self, task: TaskRecord) -> None:
        def create() -> None:
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO tasks(
                        task_id, user_id, workspace_id, workspace_name, operation, status, stage,
                        progress_json, payload_json, journal_json, result_json, error_json,
                        created_at, started_at, finished_at, modified_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.user_id,
                        task.workspace_id,
                        task.workspace_name,
                        task.operation,
                        task.status.value,
                        task.stage,
                        _json(task.progress),
                        _json(task.payload),
                        _json(task.journal),
                        _json(task.result) if task.result is not None else None,
                        _json(task.error) if task.error is not None else None,
                        _iso(task.created_at),
                        _iso(task.started_at),
                        _iso(task.finished_at),
                        _iso(task.modified_at),
                    ),
                )

        await asyncio.to_thread(create)

    async def update_task(self, task_id: str, **fields: Any) -> None:
        mapping = {
            "status": "status",
            "stage": "stage",
            "progress": "progress_json",
            "payload": "payload_json",
            "journal": "journal_json",
            "result": "result_json",
            "error": "error_json",
            "started_at": "started_at",
            "finished_at": "finished_at",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            column = mapping.get(key)
            if column is None:
                raise ValueError(f"unsupported task field: {key}")
            if key == "status" and isinstance(value, TaskStatus):
                value = value.value
            elif key in {"progress", "payload", "journal", "result", "error"}:
                value = _json(value) if value is not None else None
            elif isinstance(value, datetime):
                value = _iso(value)
            assignments.append(f"{column} = ?")
            values.append(value)
        assignments.append("modified_at = ?")
        values.append(_iso(local_now()))
        values.append(task_id)

        def update() -> None:
            with self._connect() as db:
                cursor = db.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", values)
                if cursor.rowcount != 1:
                    raise KeyError(f"task not found: {task_id}")

        await asyncio.to_thread(update)

    @staticmethod
    def _task(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            user_id=row["user_id"],
            workspace_id=row["workspace_id"],
            workspace_name=row["workspace_name"],
            operation=row["operation"],
            status=row["status"],
            stage=row["stage"],
            progress=TaskProgress.model_validate(_loads(row["progress_json"], {})),
            payload=_loads(row["payload_json"], {}),
            journal=_loads(row["journal_json"], {}),
            result=_loads(row["result_json"], None),
            error=_loads(row["error_json"], None),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            modified_at=row["modified_at"],
        )

    async def get_task(self, task_id: str) -> TaskRecord | None:
        def read() -> TaskRecord | None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                return self._task(row) if row else None

        return await asyncio.to_thread(read)

    async def queued_task_ids(self) -> list[str]:
        def read() -> list[str]:
            with self._connect() as db:
                return [row[0] for row in db.execute("SELECT task_id FROM tasks WHERE status = 'queued'")]

        return await asyncio.to_thread(read)

    async def stale_running_tasks(self, cutoff: datetime) -> list[TaskRecord]:
        def read() -> list[TaskRecord]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM tasks WHERE status = 'running' AND modified_at < ?",
                    (_iso(cutoff),),
                ).fetchall()
                return [self._task(row) for row in rows]

        return await asyncio.to_thread(read)

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        def read() -> WorkspaceRecord | None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
                return WorkspaceRecord.model_validate(dict(row)) if row else None

        return await asyncio.to_thread(read)

    async def create_workspace(self, workspace: WorkspaceRecord) -> None:
        """Create an empty workspace without changing the legacy ingestion contract."""

        def write() -> None:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO users(user_id, user_name, created_at, modified_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        workspace.user_id,
                        workspace.user_id,
                        _iso(workspace.created_at),
                        _iso(workspace.created_at),
                    ),
                )
                db.execute(
                    "INSERT INTO workspaces(workspace_id, user_id, workspace_name, resource_count, "
                    "file_count, str_count, total_size_bytes, created_at, modified_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace.workspace_id,
                        workspace.user_id,
                        workspace.workspace_name,
                        workspace.resource_count,
                        workspace.file_count,
                        workspace.str_count,
                        workspace.total_size_bytes,
                        _iso(workspace.created_at),
                        _iso(workspace.modified_at),
                    ),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

        await asyncio.to_thread(write)

    async def rename_workspace(self, workspace_id: str, workspace_name: str) -> WorkspaceRecord:
        """Rename workspace and its denormalized resource rows in one transaction."""

        def write() -> WorkspaceRecord:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                now = _iso(local_now())
                cursor = db.execute(
                    "UPDATE workspaces SET workspace_name = ?, modified_at = ? WHERE workspace_id = ?",
                    (workspace_name, now, workspace_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(workspace_id)
                db.execute(
                    "UPDATE resources SET workspace_name = ?, modified_at = ? WHERE workspace_id = ?",
                    (workspace_name, now, workspace_id),
                )
                row = db.execute(
                    "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
                ).fetchone()
                db.commit()
                return WorkspaceRecord.model_validate(dict(row))
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

        return await asyncio.to_thread(write)

    async def workspace_has_active_tasks(self, workspace_id: str) -> bool:
        def read() -> bool:
            with self._connect() as db:
                row = db.execute(
                    "SELECT 1 FROM tasks WHERE workspace_id = ? AND status IN ('queued', 'running') LIMIT 1",
                    (workspace_id,),
                ).fetchone()
                return row is not None

        return await asyncio.to_thread(read)

    async def delete_workspace(self, workspace_id: str) -> WorkspaceRecord:
        """Delete authoritative rows and adjust the owning user's aggregate counters."""

        def write() -> WorkspaceRecord:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(workspace_id)
                workspace = WorkspaceRecord.model_validate(dict(row))
                db.execute("DELETE FROM resources WHERE workspace_id = ?", (workspace_id,))
                db.execute("DELETE FROM tasks WHERE workspace_id = ?", (workspace_id,))
                db.execute("DELETE FROM repair_blocks WHERE workspace_id = ?", (workspace_id,))
                cursor = db.execute("DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,))
                if cursor.rowcount != 1:
                    raise KeyError(workspace_id)
                db.execute(
                    "UPDATE users SET resource_count = resource_count - ?, file_count = file_count - ?, "
                    "str_count = str_count - ?, total_size_bytes = total_size_bytes - ?, modified_at = ? "
                    "WHERE user_id = ?",
                    (
                        workspace.resource_count,
                        workspace.file_count,
                        workspace.str_count,
                        workspace.total_size_bytes,
                        _iso(local_now()),
                        workspace.user_id,
                    ),
                )
                db.commit()
                return workspace
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

        return await asyncio.to_thread(write)

    async def list_workspaces(self, user_id: str) -> WorkspaceListResponse:
        def read() -> WorkspaceListResponse:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM workspaces WHERE user_id = ? ORDER BY workspace_name COLLATE BINARY ASC",
                    (user_id,),
                ).fetchall()
                return WorkspaceListResponse(
                    user_id=user_id,
                    workspaces=[
                        WorkspaceListItem.model_validate({key: row[key] for key in WorkspaceListItem.model_fields})
                        for row in rows
                    ],
                )

        return await asyncio.to_thread(read)

    async def list_users(self) -> UserListResponse:
        """列出已有用户及其基础统计，供 WebUI 选择当前身份。"""

        def read() -> UserListResponse:
            with self._connect() as db:
                rows = db.execute(
                    """
                    SELECT
                        users.user_id,
                        users.user_name,
                        COUNT(workspaces.workspace_id) AS workspace_count,
                        users.resource_count,
                        users.file_count,
                        users.str_count,
                        users.total_size_bytes
                    FROM users
                    LEFT JOIN workspaces ON workspaces.user_id = users.user_id
                    GROUP BY users.user_id, users.user_name
                    ORDER BY users.user_id COLLATE BINARY ASC
                    """
                ).fetchall()
                return UserListResponse(users=[UserListItem.model_validate(dict(row)) for row in rows])

        return await asyncio.to_thread(read)

    async def create_user(self, user_id: str, user_name: str) -> UserListItem | None:
        """创建一个空用户；ID 冲突时返回 None。"""

        def write() -> UserListItem | None:
            now = _iso(local_now())
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO users(user_id, user_name, created_at, modified_at) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, user_name, now, now),
                )
                if cursor.rowcount == 0:
                    return None
                return UserListItem(user_id=user_id, user_name=user_name)

        return await asyncio.to_thread(write)

    async def delete_user(self, user_id: str) -> UserListItem:
        """删除空知识用户；仍有 Workspace 时由外键和显式校验共同拒绝。"""

        def write() -> UserListItem:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT user_id, user_name, resource_count, file_count, str_count, total_size_bytes "
                    "FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(user_id)
                if db.execute(
                    "SELECT 1 FROM workspaces WHERE user_id = ? LIMIT 1", (user_id,)
                ).fetchone():
                    raise sqlite3.IntegrityError("knowledge user still has workspaces")
                deleted = UserListItem.model_validate(dict(row))
                cursor = db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
                if cursor.rowcount != 1:
                    raise KeyError(user_id)
                db.commit()
                return deleted
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

        return await asyncio.to_thread(write)

    async def rename_user(self, user_id: str, user_name: str) -> UserListItem:
        """修改知识域展示名称，稳定保留 UserID、Workspace 和底层数据。"""

        def write() -> UserListItem:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    "UPDATE users SET user_name = ?, modified_at = ? WHERE user_id = ?",
                    (user_name, _iso(local_now()), user_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(user_id)
                row = db.execute(
                    "SELECT user_id, user_name, resource_count, file_count, str_count, total_size_bytes "
                    "FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                db.commit()
                return UserListItem.model_validate(dict(row))
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

        return await asyncio.to_thread(write)

    @staticmethod
    def _resource(row: sqlite3.Row) -> ResourceRecord:
        return ResourceRecord(
            row_id=row["row_id"],
            document_id=UUID(row["document_id"]),
            workspace_id=row["workspace_id"],
            user_id=row["user_id"],
            workspace_name=row["workspace_name"],
            source_type=row["resource_type"],
            file_id=row["file_id"],
            file_name=row["name"],
            mime_type=row["mime_type"],
            content_hash=row["content_hash"],
            size_bytes=row["size_bytes"],
            markdown_hash=row["markdown_hash"],
            parser=row["parser"],
            degraded=bool(row["degraded"]),
            chunk_count=row["chunk_count"],
            artifact_path=row["artifact_path"],
            created_at=row["created_at"],
            modified_at=row["modified_at"],
        )

    async def find_duplicate(self, workspace_id: str, source_type: str, digest: str) -> ResourceRecord | None:
        def read() -> ResourceRecord | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT * FROM resources WHERE workspace_id = ? AND resource_type = ? "
                    "AND content_hash = ? AND ingestion_complete = 1",
                    (workspace_id, source_type, digest),
                ).fetchone()
                return self._resource(row) if row else None

        return await asyncio.to_thread(read)

    async def get_file(self, workspace_id: str, file_id: str) -> ResourceRecord | None:
        def read() -> ResourceRecord | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT * FROM resources WHERE workspace_id = ? AND resource_type = 'file' AND file_id = ?",
                    (workspace_id, file_id),
                ).fetchone()
                return self._resource(row) if row else None

        return await asyncio.to_thread(read)

    async def get_string(self, workspace_id: str, content_hash: str) -> ResourceRecord | None:
        def read() -> ResourceRecord | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT * FROM resources WHERE workspace_id = ? AND resource_type = 'str' "
                    "AND content_hash = ?",
                    (workspace_id, content_hash),
                ).fetchone()
                return self._resource(row) if row else None

        return await asyncio.to_thread(read)

    async def get_resource_by_document(self, workspace_id: str, document_id: UUID) -> ResourceRecord | None:
        def read() -> ResourceRecord | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT * FROM resources WHERE workspace_id = ? AND document_id = ?",
                    (workspace_id, str(document_id)),
                ).fetchone()
                return self._resource(row) if row else None

        return await asyncio.to_thread(read)

    async def block_workspace(self, workspace_id: str, reason: str) -> None:
        def write() -> None:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO repair_blocks(workspace_id, reason, created_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET "
                    "reason = excluded.reason, created_at = excluded.created_at",
                    (workspace_id, reason[:2000], _iso(local_now())),
                )

        await asyncio.to_thread(write)

    async def workspace_repair_reason(self, workspace_id: str) -> str | None:
        def read() -> str | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT reason FROM repair_blocks WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                return str(row[0]) if row else None

        return await asyncio.to_thread(read)

    async def workspace_lifecycle_state(self, workspace_id: str) -> str | None:
        """Read the optional WebUI tombstone without requiring WebUI tables for legacy deployments."""

        def read() -> str | None:
            with self._connect() as db:
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'webui_workspace_lifecycle'"
                ).fetchone()
                if not exists:
                    return None
                row = db.execute(
                    "SELECT state FROM webui_workspace_lifecycle WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                return str(row[0]) if row else None

        return await asyncio.to_thread(read)

    async def list_resources(
        self, workspace_id: str
    ) -> tuple[list[FileListItem], list[StringListItem], WorkspaceStats]:
        def read() -> tuple[list[FileListItem], list[StringListItem], WorkspaceStats]:
            with self._connect() as db:
                workspace = db.execute("SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
                if workspace is None:
                    raise KeyError(workspace_id)
                rows = db.execute(
                    "SELECT * FROM resources WHERE workspace_id = ? AND ingestion_complete = 1 "
                    "ORDER BY created_at ASC, row_id ASC",
                    (workspace_id,),
                ).fetchall()
                files = [
                    FileListItem(
                        file_id=row["file_id"],
                        file_name=row["name"],
                        content_hash=row["content_hash"],
                        size_bytes=row["size_bytes"],
                        created_at=row["created_at"],
                        modified_at=row["modified_at"],
                    )
                    for row in rows
                    if row["resource_type"] == "file"
                ]
                strings = [
                    StringListItem(
                        content_hash=row["content_hash"],
                        size_bytes=row["size_bytes"],
                        created_at=row["created_at"],
                        modified_at=row["modified_at"],
                    )
                    for row in rows
                    if row["resource_type"] == "str"
                ]
                stats = WorkspaceStats(
                    resource_count=len(rows),
                    file_count=len(files),
                    str_count=len(strings),
                    total_size_bytes=sum(int(row["size_bytes"]) for row in rows),
                )
                return files, strings, stats

        return await asyncio.to_thread(read)

    async def list_resource_records(self, workspace_id: str) -> list[ResourceRecord]:
        def read() -> list[ResourceRecord]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM resources WHERE workspace_id = ? AND ingestion_complete = 1 "
                    "ORDER BY created_at ASC, row_id ASC",
                    (workspace_id,),
                ).fetchall()
                return [self._resource(row) for row in rows]

        return await asyncio.to_thread(read)

    async def incomplete_resource_records(self, workspace_id: str) -> list[ResourceRecord]:
        def read() -> list[ResourceRecord]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM resources WHERE workspace_id = ? AND ingestion_complete = 0 "
                    "ORDER BY created_at ASC, row_id ASC",
                    (workspace_id,),
                ).fetchall()
                return [self._resource(row) for row in rows]

        return await asyncio.to_thread(read)

    async def existing_document_ids(self, workspace_id: str, document_ids: Iterable[UUID]) -> set[UUID]:
        values = [str(value) for value in document_ids]
        if not values:
            return set()

        def read() -> set[UUID]:
            placeholders = ",".join("?" for _ in values)
            with self._connect() as db:
                rows = db.execute(
                    f"SELECT document_id FROM resources WHERE workspace_id = ? AND ingestion_complete = 1 "
                    f"AND document_id IN ({placeholders})",
                    [workspace_id, *values],
                ).fetchall()
                return {UUID(row[0]) for row in rows}

        return await asyncio.to_thread(read)

    async def all_resources(self) -> list[ResourceRecord]:
        def read() -> list[ResourceRecord]:
            with self._connect() as db:
                return [
                    self._resource(row)
                    for row in db.execute(
                        "SELECT * FROM resources WHERE ingestion_complete = 1 ORDER BY row_id"
                    )
                ]

        return await asyncio.to_thread(read)

    @staticmethod
    def _ensure_identity(db: sqlite3.Connection, resource: ResourceRecord) -> None:
        lifecycle_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'webui_workspace_lifecycle'"
        ).fetchone()
        if lifecycle_table:
            lifecycle = db.execute(
                "SELECT state FROM webui_workspace_lifecycle WHERE workspace_id = ?",
                (resource.workspace_id,),
            ).fetchone()
            if lifecycle and lifecycle["state"] in {"deleting", "delete_failed", "deleted"}:
                raise ValueError("workspace lifecycle blocks writes")
        now = _iso(resource.created_at)
        db.execute(
            "INSERT OR IGNORE INTO users(user_id, user_name, created_at, modified_at) VALUES (?, ?, ?, ?)",
            (resource.user_id, resource.user_id, now, now),
        )
        workspace = db.execute(
            "SELECT user_id, workspace_name FROM workspaces WHERE workspace_id = ?",
            (resource.workspace_id,),
        ).fetchone()
        if workspace is None:
            db.execute(
                "INSERT INTO workspaces(workspace_id, user_id, workspace_name, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (resource.workspace_id, resource.user_id, resource.workspace_name, now, now),
            )
        elif workspace["user_id"] != resource.user_id or workspace["workspace_name"] != resource.workspace_name:
            raise ValueError("workspace identity mismatch")

    @staticmethod
    def _insert_resource(
        db: sqlite3.Connection,
        resource: ResourceRecord,
        *,
        source_task_id: str | None = None,
        ingestion_complete: bool = True,
    ) -> int:
        SQLiteStore._ensure_identity(db, resource)
        cursor = db.execute(
            """
            INSERT INTO resources(
                document_id, workspace_id, user_id, workspace_name, resource_type, file_id, name,
                mime_type, content_hash, size_bytes, markdown_hash, parser, degraded, chunk_count,
                artifact_path, source_task_id, ingestion_complete, created_at, modified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(resource.document_id),
                resource.workspace_id,
                resource.user_id,
                resource.workspace_name,
                resource.source_type,
                resource.file_id,
                resource.file_name,
                resource.mime_type,
                resource.content_hash,
                resource.size_bytes,
                resource.markdown_hash,
                resource.parser,
                int(resource.degraded),
                resource.chunk_count,
                resource.artifact_path,
                source_task_id,
                int(ingestion_complete),
                _iso(resource.created_at),
                _iso(resource.modified_at),
            ),
        )
        file_delta = 1 if resource.source_type == "file" else 0
        str_delta = 1 if resource.source_type == "str" else 0
        now = _iso(resource.modified_at)
        db.execute(
            "UPDATE workspaces SET resource_count = resource_count + 1, file_count = file_count + ?, "
            "str_count = str_count + ?, total_size_bytes = total_size_bytes + ?, modified_at = ? "
            "WHERE workspace_id = ?",
            (file_delta, str_delta, resource.size_bytes, now, resource.workspace_id),
        )
        db.execute(
            "UPDATE users SET resource_count = resource_count + 1, file_count = file_count + ?, "
            "str_count = str_count + ?, total_size_bytes = total_size_bytes + ?, modified_at = ? WHERE user_id = ?",
            (file_delta, str_delta, resource.size_bytes, now, resource.user_id),
        )
        return int(cursor.lastrowid)

    async def add_resource(self, resource: ResourceRecord, finalize: Callable[[], None]) -> int:
        def write() -> int:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                row_id = self._insert_resource(db, resource)
                finalize()
                db.commit()
                return row_id
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

        return await asyncio.to_thread(write)

    async def add_resource_and_complete_task(
        self,
        resource: ResourceRecord,
        task: TaskRecord,
        result: dict[str, Any],
        finished_at: datetime,
    ) -> int:
        """Atomically make a resource visible and mark its ingestion task 100% succeeded."""

        def write() -> int:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                row_id = self._insert_resource(
                    db,
                    resource,
                    source_task_id=task.task_id,
                    ingestion_complete=True,
                )
                resource.row_id = row_id
                journal = {
                    **task.journal,
                    "metadata_written": True,
                    "resource": resource.model_dump(mode="json"),
                }
                cursor = db.execute(
                    "UPDATE tasks SET status = 'succeeded', stage = 'completed', progress_json = ?, "
                    "journal_json = ?, result_json = ?, error_json = NULL, finished_at = ?, modified_at = ? "
                    "WHERE task_id = ? AND status = 'running'",
                    (
                        _json(TaskProgress(current=100, total=100, percent=100.0)),
                        _json(journal),
                        _json(result),
                        _iso(finished_at),
                        _iso(finished_at),
                        task.task_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("ingestion task is not running")
                db.commit()
                return row_id
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

        return await asyncio.to_thread(write)

    async def restore_resource(self, resource: ResourceRecord) -> int:
        def write() -> int:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row_id = self._insert_resource(db, resource)
                db.commit()
                return row_id

        return await asyncio.to_thread(write)

    async def delete_resource(self, resource: ResourceRecord) -> None:
        def delete() -> None:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute("DELETE FROM resources WHERE row_id = ?", (resource.row_id,))
                if cursor.rowcount != 1:
                    raise KeyError(resource.row_id)
                file_delta = 1 if resource.source_type == "file" else 0
                str_delta = 1 if resource.source_type == "str" else 0
                now = _iso(local_now())
                db.execute(
                    "UPDATE workspaces SET resource_count = resource_count - 1, file_count = file_count - ?, "
                    "str_count = str_count - ?, total_size_bytes = total_size_bytes - ?, modified_at = ? "
                    "WHERE workspace_id = ?",
                    (file_delta, str_delta, resource.size_bytes, now, resource.workspace_id),
                )
                db.execute(
                    "UPDATE users SET resource_count = resource_count - 1, file_count = file_count - ?, "
                    "str_count = str_count - ?, total_size_bytes = total_size_bytes - ?, modified_at = ? "
                    "WHERE user_id = ?",
                    (file_delta, str_delta, resource.size_bytes, now, resource.user_id),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

        await asyncio.to_thread(delete)

    async def cleanup_tasks(self, retention_days: int) -> int:
        cutoff = _iso(local_now() - timedelta(days=retention_days))

        def delete() -> int:
            with self._connect() as db:
                cursor = db.execute(
                    "DELETE FROM tasks WHERE status IN ('succeeded', 'failed') AND finished_at < ?",
                    (cutoff,),
                )
                return cursor.rowcount

        return await asyncio.to_thread(delete)
