"""验证 Workspace 改名与整库删除的跨存储一致性。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from medrag_nexus.core.models import WorkspaceRecord, local_now
from medrag_nexus.storage.files import ArtifactStore
from medrag_nexus.storage.sqlite import SQLiteStore
from medrag_nexus.webui import WebUiStore, build_default_registry, create_webui_router
from medrag_nexus.webui.knowledge_router import create_knowledge_router
from medrag_nexus.webui.policy_store import KnowledgePolicyStore
from medrag_nexus.webui.security import PasswordService


class _Tasks:
    @asynccontextmanager
    async def workspace_lock(self, _user_id: str, _workspace_id: str):
        yield


class _StatefulElasticsearch:
    def __init__(self, workspace: WorkspaceRecord) -> None:
        self.workspaces = {workspace.workspace_id: workspace.model_copy(deep=True)}
        self.documents = {
            "document-1": {
                "workspace_id": workspace.workspace_id,
                "workspace_name": workspace.workspace_name,
            }
        }
        self.chunks = {
            "chunk-1": {
                "workspace_id": workspace.workspace_id,
                "workspace_name": workspace.workspace_name,
            }
        }

    async def rename_workspace(self, workspace: WorkspaceRecord) -> None:
        self.workspaces[workspace.workspace_id] = workspace.model_copy(deep=True)
        for values in (*self.documents.values(), *self.chunks.values()):
            if values["workspace_id"] == workspace.workspace_id:
                values["workspace_name"] = workspace.workspace_name

    async def delete_workspace_contents(self, workspace_id: str) -> None:
        self.workspaces.pop(workspace_id, None)
        self.documents = {
            key: value for key, value in self.documents.items() if value["workspace_id"] != workspace_id
        }
        self.chunks = {key: value for key, value in self.chunks.items() if value["workspace_id"] != workspace_id}

    async def count_workspace_contents(self, workspace_id: str) -> tuple[int, int]:
        documents = sum(value["workspace_id"] == workspace_id for value in self.documents.values())
        chunks = sum(value["workspace_id"] == workspace_id for value in self.chunks.values())
        return documents, chunks


class _StatefulMilvus:
    def __init__(self, workspace_id: str) -> None:
        self.rows = [{"workspace_id": workspace_id, "vector_id": "vector-1"}]

    async def delete_workspace(self, workspace_id: str) -> None:
        self.rows = [row for row in self.rows if row["workspace_id"] != workspace_id]

    async def count_workspace(self, workspace_id: str) -> int:
        return sum(row["workspace_id"] == workspace_id for row in self.rows)


async def test_rename_then_delete_removes_all_business_data_but_keeps_tombstone(tmp_path) -> None:
    database_path = tmp_path / "metadata.sqlite3"
    metadata = SQLiteStore(database_path)
    await metadata.ensure()
    await metadata.create_user("department-a", "部门 A")
    workspace = WorkspaceRecord(
        workspace_id="workspace_integrity",
        user_id="department-a",
        workspace_name="原知识库名称",
    )
    await metadata.create_workspace(workspace)

    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.ensure()
    original_directory = artifacts.workspace_dir(workspace.user_id, workspace.workspace_id)
    file_directory = original_directory / "files" / "file_550e8400-e29b-41d4-a716-446655440000"
    file_directory.mkdir(parents=True)
    (file_directory / "document.md").write_text("知识内容", encoding="utf-8")

    now = local_now().isoformat()
    task_id = "a" * 32
    document_id = str(uuid4())
    with sqlite3.connect(database_path) as db:
        db.execute(
            "INSERT INTO tasks(task_id, user_id, workspace_id, workspace_name, operation, status, stage, "
            "progress_json, payload_json, journal_json, result_json, error_json, created_at, started_at, "
            "finished_at, modified_at) VALUES (?, ?, ?, ?, 'add_file', 'succeeded', 'completed', ?, ?, ?, ?, "
            "NULL, ?, ?, ?, ?)",
            (
                task_id,
                workspace.user_id,
                workspace.workspace_id,
                workspace.workspace_name,
                json.dumps({"percent": 100}),
                json.dumps({"document_id": document_id}),
                "{}",
                "{}",
                now,
                now,
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO resources(document_id, workspace_id, user_id, workspace_name, resource_type, file_id, "
            "name, mime_type, content_hash, size_bytes, markdown_hash, parser, degraded, chunk_count, "
            "artifact_path, source_task_id, ingestion_complete, created_at, modified_at) "
            "VALUES (?, ?, ?, ?, 'file', ?, 'manual.pdf', 'application/pdf', ?, 12, ?, 'test', 0, 1, ?, ?, 1, ?, ?)",
            (
                document_id,
                workspace.workspace_id,
                workspace.user_id,
                workspace.workspace_name,
                "file_550e8400-e29b-41d4-a716-446655440000",
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                str(file_directory),
                task_id,
                now,
                now,
            ),
        )
        db.execute(
            "UPDATE workspaces SET resource_count=1, file_count=1, total_size_bytes=12 WHERE workspace_id=?",
            (workspace.workspace_id,),
        )
        db.execute(
            "UPDATE users SET resource_count=1, file_count=1, total_size_bytes=12 WHERE user_id=?",
            (workspace.user_id,),
        )

    registry = build_default_registry()
    accounts = WebUiStore(database_path, registry)
    policies = KnowledgePolicyStore(database_path)
    await accounts.ensure()
    await policies.ensure()
    password_hash = PasswordService().hash("admin")
    admin = await accounts.bootstrap_superadmin(
        login_name="admin",
        display_name="管理员",
        password_hash=password_hash,
    )
    token, _ = await accounts.create_session(admin, timedelta(hours=1))
    await policies.set_workspace_policy(
        workspace.workspace_id,
        read_min_level=0,
        cud_min_level=0,
        actor_account_id=admin.account_id,
        creating=True,
    )
    await policies.mark_lifecycle(workspace.workspace_id, "active", actor_account_id=admin.account_id)

    elasticsearch = _StatefulElasticsearch(workspace)
    milvus = _StatefulMilvus(workspace.workspace_id)
    original_vectors = [row.copy() for row in milvus.rows]
    runtime = SimpleNamespace(
        metadata=metadata,
        tasks=_Tasks(),
        elasticsearch=elasticsearch,
        milvus=milvus,
        artifacts=artifacts,
        settings=SimpleNamespace(max_file_bytes=50 * 1024 * 1024),
    )
    app = FastAPI()
    app.include_router(create_webui_router(accounts, registry))
    app.include_router(create_knowledge_router(runtime, accounts, policies))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_account_session", token)
        renamed = await client.patch(
            f"/api/v1/workspaces/{workspace.workspace_id}",
            json={"workspace_name": "新知识库名称"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["workspace_id"] == workspace.workspace_id
        assert original_directory.is_dir()
        assert milvus.rows == original_vectors
        stored_resources = await metadata.list_resource_records(workspace.workspace_id)
        assert stored_resources[0].workspace_name == "新知识库名称"
        assert elasticsearch.workspaces[workspace.workspace_id].workspace_name == "新知识库名称"
        assert {item["workspace_name"] for item in elasticsearch.documents.values()} == {"新知识库名称"}
        assert {item["workspace_name"] for item in elasticsearch.chunks.values()} == {"新知识库名称"}

        deleted = await client.delete(
            f"/api/v1/workspaces/{workspace.workspace_id}",
            params={"confirm_name": "新知识库名称"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "deleted"

    with sqlite3.connect(database_path) as db:
        def count(table: str) -> int:
            return int(
                db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE workspace_id=?",
                    (workspace.workspace_id,),
                ).fetchone()[0]
            )

        assert count("workspaces") == 0
        assert count("resources") == 0
        assert count("tasks") == 0
        assert count("webui_workspace_policies") == 0
        assert db.execute(
            "SELECT COUNT(*) FROM webui_policy_bindings WHERE resource_type='workspace' AND resource_id=?",
            (workspace.workspace_id,),
        ).fetchone()[0] == 0
    assert elasticsearch.workspaces == {}
    assert elasticsearch.documents == {}
    assert elasticsearch.chunks == {}
    assert milvus.rows == []
    assert not original_directory.exists()
    assert not any(artifacts.recycle_root.iterdir())
    assert await metadata.workspace_lifecycle_state(workspace.workspace_id) == "deleted"
