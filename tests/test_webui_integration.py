"""WebUI 外层门锁与后台清理任务的集成测试。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from medrag_nexus.core.models import WorkspaceRecord
from medrag_nexus.storage.files import ArtifactStore
from medrag_nexus.storage.sqlite import SQLiteStore
from medrag_nexus.webui.integration import WebUiFeature
from medrag_nexus.webui.security import WEBUI_LOCK_COOKIE_NAME, verify_webui_lock_session


def _lock_cookie(password: str, *, expires_at: int, nonce: str = "fixed-nonce") -> str:
    payload = f"v1.{expires_at}.{nonce}"
    signing_key = hmac.new(
        password.encode(),
        b"medrag-nexus:webui-session:v1",
        hashlib.sha256,
    ).digest()
    signature = hmac.new(signing_key, payload.encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{payload}.{encoded}"


def test_python_verifies_the_nextjs_outer_lock_cookie_format() -> None:
    token = _lock_cookie("outer-secret", expires_at=2_000_000_000)
    assert verify_webui_lock_session(token, "outer-secret", now=1_900_000_000)
    assert not verify_webui_lock_session(token, "wrong-secret", now=1_900_000_000)
    assert not verify_webui_lock_session(token, "outer-secret", now=2_000_000_000)


async def test_outer_lock_protects_only_webui_bff_routes(tmp_path) -> None:
    settings = SimpleNamespace(
        sqlite_path=tmp_path / "metadata.sqlite3",
        webui_cookie_secure=False,
        webui_lock_password="outer-secret",
        webui_cleanup_retry_seconds=60,
    )
    feature = WebUiFeature(SimpleNamespace(), settings)  # type: ignore[arg-type]
    app = FastAPI()
    feature.install(app)

    @app.get("/api/v1/ping")
    async def public_ping() -> dict[str, bool]:
        return {"ok": True}

    token = _lock_cookie("outer-secret", expires_at=2_000_000_000)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        direct = await client.get("/api/v1/auth/me")
        assert direct.status_code == 401
        assert direct.json()["detail"]["code"] == "outer_lock_required"

        client.cookies.set(WEBUI_LOCK_COOKIE_NAME, token)
        through_lock = await client.get("/api/v1/auth/me")
        assert through_lock.status_code == 401
        assert through_lock.json()["detail"]["code"] == "authentication_required"

        public = await client.get("/api/v1/ping")
        assert public.status_code == 200


class _CleanupElasticsearch:
    def __init__(self, completed: asyncio.Event) -> None:
        self.completed = completed

    async def delete_workspace_contents(self, _workspace_id: str) -> None:
        self.completed.set()

    async def count_workspace_contents(self, _workspace_id: str) -> tuple[int, int]:
        return 0, 0


class _CleanupMilvus:
    async def delete_workspace(self, _workspace_id: str) -> None:
        return None

    async def count_workspace(self, _workspace_id: str) -> int:
        return 0


class _CleanupArtifacts:
    async def cleanup_recycle(self, _operation_id: str) -> None:
        return None


class _CleanupTasks:
    def __init__(self) -> None:
        self.acquired: list[tuple[str, str]] = []

    @asynccontextmanager
    async def workspace_lock(self, user_id: str, workspace_id: str) -> AsyncIterator[None]:
        self.acquired.append((user_id, workspace_id))
        yield


async def test_failed_workspace_cleanup_retries_without_restarting_service(tmp_path) -> None:
    completed = asyncio.Event()
    cleanup_tasks = _CleanupTasks()
    settings = SimpleNamespace(
        sqlite_path=tmp_path / "metadata.sqlite3",
        webui_cookie_secure=False,
        webui_lock_password="",
        webui_cleanup_retry_seconds=0.01,
        webui_deletion_lease_seconds=300,
        webui_superadmin_username="",
        webui_superadmin_password="",
        webui_superadmin_display_name="超级管理员",
    )
    runtime = SimpleNamespace(
        elasticsearch=_CleanupElasticsearch(completed),
        milvus=_CleanupMilvus(),
        artifacts=_CleanupArtifacts(),
        tasks=cleanup_tasks,
    )
    feature = WebUiFeature(runtime, settings)  # type: ignore[arg-type]
    await feature.start()
    try:
        await feature.policies.mark_lifecycle(
            "workspace_failed",
            "delete_failed",
            actor_account_id="system",
            detail="workspace-delete-test|temporary failure",
        )
        await asyncio.wait_for(completed.wait(), timeout=1)
        for _ in range(20):
            if await feature.policies.lifecycle("workspace_failed") == "deleted":
                break
            await asyncio.sleep(0.01)
        assert await feature.policies.lifecycle("workspace_failed") == "deleted"
        assert cleanup_tasks.acquired == [("__webui_cleanup__", "workspace_failed")]
    finally:
        await feature.close()


async def test_empty_database_bootstraps_all_configured_superadmins_without_knowledge_users(tmp_path) -> None:
    metadata = SQLiteStore(tmp_path / "metadata.sqlite3")
    await metadata.ensure()
    settings = SimpleNamespace(
        sqlite_path=metadata.path,
        webui_cookie_secure=False,
        webui_lock_password="",
        webui_cleanup_retry_seconds=60,
        webui_deletion_lease_seconds=300,
        webui_superadmins_json=json.dumps(
            [
                {"login_name": "yqy", "display_name": "YQY", "password": "123456"},
                {"login_name": "wst", "display_name": "WST", "password": "123456"},
            ]
        ),
        webui_superadmin_username="",
        webui_superadmin_password="",
        webui_superadmin_display_name="超级管理员",
    )
    feature = WebUiFeature(SimpleNamespace(metadata=metadata), settings)  # type: ignore[arg-type]
    await feature.start()
    try:
        accounts = await feature.store.list_accounts()
        assert {item.login_name for item in accounts} == {"yqy", "wst"}
        assert all(item.permission_level == 1000 for item in accounts)
        assert all(item.groups == [] for item in accounts)
        assert all(item.bound_user_id is None for item in accounts)
        assert (await metadata.list_users()).users == []
    finally:
        await feature.close()


async def test_deleting_workspace_is_completed_after_service_restart(tmp_path) -> None:
    metadata = SQLiteStore(tmp_path / "metadata.sqlite3")
    await metadata.ensure()
    await metadata.create_user("department-a", "部门 A")
    workspace = WorkspaceRecord(
        workspace_id="workspace_restart",
        user_id="department-a",
        workspace_name="待恢复删除知识库",
    )
    await metadata.create_workspace(workspace)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    await artifacts.ensure()
    workspace_dir = artifacts.workspace_dir(workspace.user_id, workspace.workspace_id)
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "sentinel.txt").write_text("must be deleted", encoding="utf-8")

    completed = asyncio.Event()
    settings = SimpleNamespace(
        sqlite_path=metadata.path,
        webui_cookie_secure=False,
        webui_lock_password="",
        webui_cleanup_retry_seconds=60,
        webui_deletion_lease_seconds=0,
        webui_superadmin_username="",
        webui_superadmin_password="",
        webui_superadmin_display_name="超级管理员",
    )
    cleanup_tasks = _CleanupTasks()
    runtime = SimpleNamespace(
        metadata=metadata,
        elasticsearch=_CleanupElasticsearch(completed),
        milvus=_CleanupMilvus(),
        artifacts=artifacts,
        tasks=cleanup_tasks,
    )
    feature = WebUiFeature(runtime, settings)  # type: ignore[arg-type]
    await feature.store.ensure()
    await feature.policies.ensure()
    await feature.policies.mark_lifecycle(
        workspace.workspace_id,
        "deleting",
        actor_account_id="system",
        detail="workspace-delete-restart-test",
    )

    await feature.start()
    try:
        assert completed.is_set()
        assert await metadata.get_workspace(workspace.workspace_id) is None
        assert await feature.policies.lifecycle(workspace.workspace_id) == "deleted"
        assert not workspace_dir.exists()
        assert not artifacts.recycle_dir("workspace-delete-restart-test").exists()
        assert cleanup_tasks.acquired == [(workspace.user_id, workspace.workspace_id)]
        audit, _ = await feature.store.list_audit_events(limit=20)
        assert any(event.action == "webui.workspace.delete.recovered" for event in audit)
    finally:
        await feature.close()
