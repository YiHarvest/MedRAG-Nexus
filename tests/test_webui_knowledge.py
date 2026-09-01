"""验证 WebUI BFF 权限过滤和服务端推导的 Workspace 操作。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from medrag_nexus.core.ids import new_id
from medrag_nexus.core.models import ResourceRecord, TaskAccepted, TaskRecord, WorkspaceRecord, local_now
from medrag_nexus.identity import AccountStore, build_default_registry, create_account_router
from medrag_nexus.identity.security import PasswordService
from medrag_nexus.knowledge.policies import USER_POLICY_ACTIONS, KnowledgePolicyStore, PolicyBinding
from medrag_nexus.knowledge.router import create_knowledge_router
from medrag_nexus.services.files import FileService
from medrag_nexus.storage.sqlite import SQLiteStore


class FakeTasks:
    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @asynccontextmanager
    async def workspace_lock(self, user_id: str, workspace_id: str):
        lock = self._locks.setdefault((user_id, workspace_id), asyncio.Lock())
        async with lock:
            yield


class FakeElasticsearch:
    def __init__(self) -> None:
        self.workspaces: dict[str, WorkspaceRecord] = {}

    async def mirror_workspace(self, workspace: WorkspaceRecord) -> None:
        self.workspaces[workspace.workspace_id] = workspace

    async def rename_workspace(self, workspace: WorkspaceRecord) -> None:
        self.workspaces[workspace.workspace_id] = workspace

    async def delete_workspace_contents(self, workspace_id: str) -> None:
        self.workspaces.pop(workspace_id, None)

    async def count_workspace_contents(self, _workspace_id: str) -> tuple[int, int]:
        return 0, 0


class FakeMilvus:
    async def delete_workspace(self, _workspace_id: str) -> None:
        return None

    async def count_workspace(self, _workspace_id: str) -> int:
        return 0


class FakeArtifacts:
    async def move_workspace_to_recycle(self, *_args):
        return None

    async def cleanup_recycle(self, _operation_id: str) -> None:
        return None

    def raw_file_path(self, resource: ResourceRecord) -> Path:
        return Path(resource.artifact_path) / "raw" / str(resource.file_name)


async def _setup(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    metadata = SQLiteStore(path)
    await metadata.ensure()
    await metadata.create_user("department-a", "部门 A")
    await metadata.create_workspace(
        WorkspaceRecord(
            workspace_id="workspace_existing",
            user_id="department-a",
            workspace_name="公开知识库",
        )
    )
    registry = build_default_registry()
    accounts = AccountStore(path, registry)
    policies = KnowledgePolicyStore(path)
    await accounts.ensure()
    await policies.ensure()
    password = PasswordService().hash("correct-password")
    superadmin = await accounts.bootstrap_superadmin(
        login_name="admin",
        display_name="管理员",
        password_hash=password,
    )
    registered = await accounts.register_account(
        login_name="reader",
        display_name="读者",
        password_hash=password,
    )
    await policies.ensure_resource_acl("user", "department-a")
    await policies.ensure_resource_acl("workspace", "workspace_existing", user_id="department-a")
    await policies.replace_bindings(
        "user",
        "department-a",
        "webui.user.read",
        [PolicyBinding(principal_type="account", principal_id=registered.account_id, effect="allow")],
        actor_account_id=superadmin.account_id,
    )
    await policies.replace_bindings(
        "workspace",
        "workspace_existing",
        "webui.workspace.read",
        [PolicyBinding(principal_type="account", principal_id=registered.account_id, effect="allow")],
        actor_account_id=superadmin.account_id,
    )
    admin_token, _ = await accounts.create_session(superadmin, timedelta(hours=1))
    reader_token, _ = await accounts.create_session(registered, timedelta(hours=1))
    runtime = SimpleNamespace(
        metadata=metadata,
        tasks=FakeTasks(),
        elasticsearch=FakeElasticsearch(),
        milvus=FakeMilvus(),
        artifacts=FakeArtifacts(),
        settings=SimpleNamespace(max_file_bytes=1024),
    )
    app = FastAPI()
    app.state.test_runtime = runtime
    app.include_router(create_account_router(accounts, registry))
    app.include_router(create_knowledge_router(runtime, accounts, policies))
    return app, metadata, policies, admin_token, reader_token, superadmin.account_id


async def test_registered_user_only_receives_readable_workspaces_and_capabilities(tmp_path) -> None:
    app, _metadata, policies, admin_token, reader_token, admin_account_id = await _setup(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", reader_token)
        response = await client.get("/api/v1/workspaces")
        assert response.status_code == 200
        assert response.json()["users"][0]["can_delete"] is False
        assert response.json()["users"][0]["can_rename"] is False
        item = response.json()["workspaces"][0]
        assert item["workspace_id"] == "workspace_existing"
        assert item["capabilities"] == {
            "can_read": True,
            "can_add_file": False,
            "can_download_file": False,
            "can_add_text": False,
            "can_delete_file": False,
            "can_delete_text": False,
            "can_add_resource": False,
            "can_delete_resource": False,
            "can_rename": False,
            "can_delete_workspace": False,
            "can_manage_policy": False,
        }

        await policies.set_workspace_policy(
            "workspace_existing",
            read_min_level=2,
            cud_min_level=2,
            actor_account_id=admin_account_id,
        )
        hidden = await client.get("/api/v1/workspaces")
        assert hidden.json()["workspaces"] == []

        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        visible_to_admin = await client.get("/api/v1/workspaces")
        assert visible_to_admin.json()["users"][0]["can_delete"] is True
        assert visible_to_admin.json()["users"][0]["can_rename"] is True
        assert visible_to_admin.json()["workspaces"][0]["capabilities"]["can_delete_workspace"] is True


async def test_file_detail_and_raw_download_recheck_workspace_permission(tmp_path) -> None:
    app, metadata, policies, admin_token, reader_token, admin_account_id = await _setup(tmp_path)
    accounts = AccountStore(policies.path, build_default_registry())
    reader = await accounts.get_account_by_login("reader")
    assert reader is not None
    artifact = tmp_path / "artifact"
    raw = artifact / "raw" / "notes.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("download me", encoding="utf-8")
    now = local_now()
    await metadata.add_resource(
        ResourceRecord(
            document_id=new_id(),
            workspace_id="workspace_existing",
            user_id="department-a",
            workspace_name="公开知识库",
            source_type="file",
            file_id="file_11111111-1111-4111-8111-111111111111",
            file_name="notes.txt",
            mime_type="text/plain",
            content_hash="sha256:" + "a" * 32,
            size_bytes=raw.stat().st_size,
            markdown_hash="sha256:" + "b" * 32,
            parser="text",
            chunk_count=1,
            artifact_path=str(artifact),
            created_at=now,
            modified_at=now,
        ),
        lambda: None,
    )
    path = "/api/v1/workspaces/workspace_existing/files/file_11111111-1111-4111-8111-111111111111"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", reader_token)
        detail = await client.get(path)
        denied = await client.get(f"{path}/download")
        assert detail.status_code == 200
        assert detail.json()["downloadable"] is False
        assert "artifact_path" not in detail.json()
        assert denied.status_code == 404

        await policies.replace_bindings(
            "workspace",
            "workspace_existing",
            "webui.resource.file.download",
            [PolicyBinding(principal_type="account", principal_id=reader.account_id, effect="allow")],
            actor_account_id=admin_account_id,
        )
        allowed = await client.get(f"{path}/download")
        assert allowed.status_code == 200
        assert allowed.content == b"download me"
        assert "attachment" in allowed.headers["content-disposition"]

        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        admin_download = await client.get(f"{path}/download")
        assert admin_download.status_code == 200


async def test_member_can_leave_workspace_and_domain_but_superadmin_cannot(tmp_path) -> None:
    app, _metadata, _policies, admin_token, reader_token, _admin_account_id = await _setup(tmp_path)
    accounts = AccountStore(tmp_path / "metadata.sqlite3", build_default_registry())
    reader = await accounts.get_account_by_login("reader")
    assert reader is not None
    await accounts.bind_account_user(reader.account_id, "department-a", actor_account_id=reader.account_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", reader_token)
        left_workspace = await client.delete("/api/v1/workspaces/workspace_existing/access")
        assert left_workspace.status_code == 200, left_workspace.text
        after_workspace = await client.get("/api/v1/workspaces")
        assert len(after_workspace.json()["users"]) == 1
        assert after_workspace.json()["workspaces"] == []

        left_user = await client.delete("/api/v1/users/department-a/access")
        assert left_user.status_code == 200, left_user.text
        updated_reader = await accounts.get_account(reader.account_id)
        assert updated_reader is not None
        assert updated_reader.bound_user_id is None
        assert updated_reader.bound_user_ids == []
        after_user = await client.get("/api/v1/workspaces")
        assert after_user.json() == {"users": [], "workspaces": []}

        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        protected = await client.delete("/api/v1/users/department-a/access")
        assert protected.status_code == 409
        still_visible = await client.get("/api/v1/workspaces")
        assert len(still_visible.json()["users"]) == 1


async def test_superadmin_explicitly_creates_and_renames_empty_workspace(tmp_path) -> None:
    app, metadata, _policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        retired_level = await client.post(
            "/api/v1/workspaces",
            json={
                "user_id": "department-a",
                "workspace_name": "旧等级知识库",
                "read_min_level": 3,
                "cud_min_level": 3,
            },
        )
        assert retired_level.status_code == 422
        assert retired_level.json()["detail"]["code"] == "invalid_permission_level"

        created = await client.post(
            "/api/v1/workspaces",
            json={
                "user_id": "department-a",
                "workspace_name": "内部制度",
                "read_min_level": 1,
                "cud_min_level": 2,
            },
        )
        assert created.status_code == 201, created.text
        workspace_id = created.json()["workspace_id"]
        assert (await metadata.get_workspace(workspace_id)).workspace_name == "内部制度"  # type: ignore[union-attr]

        renamed = await client.patch(
            f"/api/v1/workspaces/{workspace_id}",
            json={"workspace_name": "内部规章制度"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["workspace_name"] == "内部规章制度"
        assert (await metadata.get_workspace(workspace_id)).workspace_name == "内部规章制度"  # type: ignore[union-attr]

        deleted = await client.delete(
            f"/api/v1/workspaces/{workspace_id}",
            params={"confirm_name": "内部规章制度"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"status": "deleted", "workspace_id": workspace_id}
        assert await metadata.get_workspace(workspace_id) is None
        assert await metadata.workspace_lifecycle_state(workspace_id) == "deleted"


async def test_superadmin_renames_knowledge_domain_without_changing_ids(tmp_path) -> None:
    app, metadata, _policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        renamed = await client.patch(
            "/api/v1/users/department-a",
            json={"user_name": "产品知识域"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["user_id"] == "department-a"
        assert renamed.json()["user_name"] == "产品知识域"

    users = (await metadata.list_users()).users
    assert [(item.user_id, item.user_name) for item in users] == [("department-a", "产品知识域")]
    workspace = await metadata.get_workspace("workspace_existing")
    assert workspace is not None
    assert workspace.user_id == "department-a"


async def test_superadmin_can_create_multiple_knowledge_domains(tmp_path) -> None:
    app, metadata, _policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    created_ids: list[str] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        for name in ("产品资料", "法务资料", "培训资料"):
            response = await client.post("/api/v1/users", json={"user_name": name})
            assert response.status_code == 201, response.text
            created_ids.append(response.json()["user_id"])

        caller_selected_id = await client.post(
            "/api/v1/users",
            json={"user_id": "caller-selected", "user_name": "不允许指定 ID"},
        )
        assert caller_selected_id.status_code == 422

        caller_selected_workspace_id = await client.post(
            "/api/v1/workspaces",
            json={
                "workspace_id": "caller-selected",
                "user_id": created_ids[0],
                "workspace_name": "不允许指定 ID",
            },
        )
        assert caller_selected_workspace_id.status_code == 422

        visible = await client.get("/api/v1/workspaces")
        assert visible.status_code == 200
        visible_ids = {item["user_id"] for item in visible.json()["users"]}
        assert set(created_ids) <= visible_ids

    persisted_ids = {item.user_id for item in (await metadata.list_users()).users}
    assert set(created_ids) <= persisted_ids


async def test_custom_group_can_grant_knowledge_domain_creation(tmp_path) -> None:
    app, metadata, _policies, admin_token, reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        group = await client.post(
            "/api/v1/permission-groups",
            json={
                "group_key": "webui.custom.domain_creator",
                "name": "知识域创建者",
                "description": "允许新建知识域",
                "permissions": ["webui.user.create"],
            },
        )
        assert group.status_code == 201, group.text
        accounts = (await client.get("/api/v1/accounts")).json()["accounts"]
        reader = next(item for item in accounts if item["login_name"] == "reader")
        updated = await client.patch(
            f"/api/v1/accounts/{reader['account_id']}",
            json={"group_keys": ["webui.custom.domain_creator"]},
        )
        assert updated.status_code == 200, updated.text

        client.cookies.set("medrag_nexus_webui_account_session", reader_token)
        created = await client.post(
            "/api/v1/users",
            json={"user_name": "读者创建的知识域"},
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["user_id"]
        visible = await client.get("/api/v1/workspaces")
        assert user_id in {item["user_id"] for item in visible.json()["users"]}

    assert user_id in {item.user_id for item in (await metadata.list_users()).users}


async def test_superadmin_cascade_deletes_knowledge_user_and_reader_cannot(tmp_path) -> None:
    app, metadata, policies, admin_token, reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", reader_token)
        denied = await client.delete(
            "/api/v1/users/department-a",
            params={"confirm_name": "部门 A"},
        )
        assert denied.status_code == 404

        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        mismatch = await client.delete(
            "/api/v1/users/department-a",
            params={"confirm_name": "错误名称"},
        )
        assert mismatch.status_code == 422

        deleted = await client.delete(
            "/api/v1/users/department-a",
            params={"confirm_name": "部门 A"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {
            "status": "deleted",
            "user_id": "department-a",
            "deleted_workspace_count": 1,
        }

    assert (await metadata.list_users()).users == []
    assert await metadata.get_workspace("workspace_existing") is None
    assert all(
        not bindings
        for bindings in (await policies.list_bindings("user", "department-a", USER_POLICY_ACTIONS)).values()
    )


async def test_concurrent_workspace_deletes_preserve_deleted_tombstone(tmp_path) -> None:
    app, metadata, policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        responses = await asyncio.gather(
            client.delete(
                "/api/v1/workspaces/workspace_existing",
                params={"confirm_name": "公开知识库"},
            ),
            client.delete(
                "/api/v1/workspaces/workspace_existing",
                params={"confirm_name": "公开知识库"},
            ),
        )

    assert sum(response.status_code == 200 for response in responses) == 1
    assert all(response.status_code in {200, 404, 409} for response in responses)
    assert await metadata.get_workspace("workspace_existing") is None
    assert await policies.lifecycle("workspace_existing") == "deleted"


async def test_workspace_acl_is_allowlist_and_explicit_deny_wins(tmp_path) -> None:
    app, _metadata, _policies, admin_token, reader_token, admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={"login_name": "outsider", "display_name": "局外人", "password": "OutsiderSecure!123"},
        )
        assert registered.status_code == 201
        outsider_token = client.cookies.get("medrag_nexus_webui_account_session")

        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        accounts = (await client.get("/api/v1/accounts")).json()["accounts"]
        reader_id = next(item["account_id"] for item in accounts if item["login_name"] == "reader")
        allow = await client.put(
            "/api/v1/workspaces/workspace_existing/bindings",
            json={
                "action": "webui.workspace.read",
                "bindings": [{"principal_type": "account", "principal_id": reader_id, "effect": "allow"}],
            },
        )
        assert allow.status_code == 200, allow.text
        assert allow.json()["bindings"]["webui.workspace.delete"] == [
            {
                "principal_type": "account",
                "principal_id": admin_account_id,
                "effect": "allow",
                "immutable": True,
                "managed_by": "system.superadmin",
            }
        ]

        client.cookies.set("medrag_nexus_webui_account_session", reader_token)
        assert len((await client.get("/api/v1/workspaces")).json()["workspaces"]) == 1
        client.cookies.set("medrag_nexus_webui_account_session", outsider_token)
        assert (await client.get("/api/v1/workspaces")).json()["workspaces"] == []

        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        denied = await client.put(
            "/api/v1/workspaces/workspace_existing/bindings",
            json={
                "action": "webui.workspace.read",
                "bindings": [
                    {"principal_type": "account", "principal_id": reader_id, "effect": "deny"},
                ],
            },
        )
        assert denied.status_code == 200
        client.cookies.set("medrag_nexus_webui_account_session", reader_token)
        assert (await client.get("/api/v1/workspaces")).json()["workspaces"] == []


async def test_acl_allow_cannot_grant_an_action_missing_from_the_target_group(tmp_path) -> None:
    app, _metadata, _policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        created = await client.post(
            "/api/v1/permission-groups",
            json={
                "group_key": "webui.custom.read_only",
                "name": "只读组",
                "description": "只能查看知识内容",
                "permissions": ["webui.user.read", "webui.workspace.read"],
            },
        )
        assert created.status_code == 201, created.text

        valid = await client.put(
            "/api/v1/users/department-a/bindings",
            json={
                "action": "webui.user.read",
                "bindings": [{"principal_type": "group", "principal_id": "webui.custom.read_only", "effect": "allow"}],
            },
        )
        assert valid.status_code == 200, valid.text

        ineffective = await client.put(
            "/api/v1/users/department-a/bindings",
            json={
                "action": "webui.workspace.create",
                "bindings": [{"principal_type": "group", "principal_id": "webui.custom.read_only", "effect": "allow"}],
            },
        )
        assert ineffective.status_code == 422
        assert ineffective.json()["detail"]["code"] == "ineffective_policy_binding"


async def test_user_without_cud_cannot_mutate_workspace_or_resources(tmp_path) -> None:
    app, _metadata, _policies, _admin_token, reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", reader_token)
        responses = [
            await client.patch(
                "/api/v1/workspaces/workspace_existing",
                json={"workspace_name": "不允许改名"},
            ),
            await client.delete(
                "/api/v1/workspaces/workspace_existing",
                params={"confirm_name": "公开知识库"},
            ),
            await client.post(
                "/api/v1/workspaces/workspace_existing/resources",
                data={"type": "str", "content": "不允许上传"},
            ),
            await client.delete(
                "/api/v1/workspaces/workspace_existing/files/file_missing",
            ),
            await client.delete(
                "/api/v1/workspaces/workspace_existing/strings/hash_missing",
            ),
        ]
    assert all(response.status_code == 404 for response in responses)


async def test_file_and_text_resource_permissions_are_independent(tmp_path, monkeypatch) -> None:
    app, _metadata, policies, _admin_token, _reader_token, admin_account_id = await _setup(tmp_path)
    registry = build_default_registry()
    accounts = AccountStore(policies.path, registry)
    await accounts.create_permission_group(
        group_key="webui.text_only",
        description="仅可添加文本",
        permissions=[
            "webui.user.read",
            "webui.workspace.read",
            "webui.resource.text.add",
        ],
        actor_account_id=admin_account_id,
    )
    text_editor = await accounts.create_account(
        login_name="text-editor",
        display_name="文本编辑者",
        password_hash=PasswordService().hash("TextEditorSecure!123"),
        permission_level=0,
        group_keys=["webui.text_only"],
        bound_user_id=None,
        must_change_password=False,
        actor_account_id=admin_account_id,
    )
    await policies.set_workspace_policy(
        "workspace_existing",
        read_min_level=0,
        cud_min_level=0,
        actor_account_id=admin_account_id,
    )
    for resource_type, resource_id, action in (
        ("user", "department-a", "webui.user.read"),
        ("workspace", "workspace_existing", "webui.workspace.read"),
        ("workspace", "workspace_existing", "webui.resource.text.add"),
    ):
        await policies.replace_bindings(
            resource_type,
            resource_id,
            action,
            [PolicyBinding(principal_type="account", principal_id=text_editor.account_id, effect="allow")],
            actor_account_id=admin_account_id,
        )
    token, _ = await accounts.create_session(text_editor, timedelta(hours=1))
    monkeypatch.setattr(FileService, "submit_add", AsyncMock(return_value=TaskAccepted(task_id="c" * 32)))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", token)
        knowledge = await client.get("/api/v1/workspaces")
        capabilities = knowledge.json()["workspaces"][0]["capabilities"]
        assert capabilities["can_add_text"] is True
        assert capabilities["can_add_file"] is False
        assert capabilities["can_add_resource"] is True
        text_response = await client.post(
            "/api/v1/workspaces/workspace_existing/resources",
            data={"type": "str", "content": "仅允许添加的文本"},
        )
        file_response = await client.post(
            "/api/v1/workspaces/workspace_existing/resources",
            data={"type": "file"},
        )

    assert text_response.status_code == 202
    assert file_response.status_code == 404


async def test_direct_workspace_routes_cannot_bypass_hidden_user_scope(tmp_path) -> None:
    app, metadata, policies, _admin_token, _reader_token, admin_account_id = await _setup(tmp_path)
    accounts = AccountStore(policies.path, build_default_registry())
    manager = await accounts.create_account(
        login_name="manager",
        display_name="Workspace Manager",
        password_hash=PasswordService().hash("ManagerSecure!123"),
        permission_level=2,
        group_keys=[],
        bound_user_id=None,
        must_change_password=False,
        actor_account_id=admin_account_id,
    )
    manager_token, _ = await accounts.create_session(manager, timedelta(hours=1))
    await policies.replace_bindings(
        "user",
        "department-a",
        "webui.user.read",
        [PolicyBinding(principal_type="account", principal_id=manager.account_id, effect="deny")],
        actor_account_id=admin_account_id,
    )
    task_id = "d" * 32
    await metadata.create_task(
        TaskRecord(
            task_id=task_id,
            user_id="department-a",
            workspace_id="workspace_existing",
            workspace_name="公开知识库",
            operation="retrieval",
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", manager_token)
        responses = [
            await client.patch(
                "/api/v1/workspaces/workspace_existing",
                json={"workspace_name": "越权改名"},
            ),
            await client.put(
                "/api/v1/workspaces/workspace_existing/policy",
                json={"read_min_level": 0, "cud_min_level": 0},
            ),
            await client.get("/api/v1/workspaces/workspace_existing/bindings"),
            await client.put(
                "/api/v1/workspaces/workspace_existing/bindings",
                json={"action": "read", "bindings": []},
            ),
            await client.get("/api/v1/workspaces/workspace_existing/files"),
            await client.post(
                "/api/v1/workspaces/workspace_existing/resources",
                data={"type": "str", "content": "越权内容"},
            ),
            await client.delete("/api/v1/workspaces/workspace_existing/files/file_missing"),
            await client.delete("/api/v1/workspaces/workspace_existing/strings/hash_missing"),
            await client.post(
                "/api/v1/retrieval",
                json={"workspace_id": "workspace_existing", "query": "越权检索"},
            ),
            await client.get(f"/api/v1/tasks/{task_id}"),
            await client.delete(
                "/api/v1/workspaces/workspace_existing",
                params={"confirm_name": "公开知识库"},
            ),
        ]
    assert all(response.status_code == 404 for response in responses)
    assert await metadata.get_workspace("workspace_existing") is not None


async def test_superadmin_sees_all_accounts_users_and_workspaces(tmp_path) -> None:
    app, _metadata, _policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={"login_name": "second", "display_name": "第二用户", "password": "SecondSecure!123"},
        )
        assert registered.status_code == 201
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        accounts = await client.get("/api/v1/accounts")
        knowledge = await client.get("/api/v1/workspaces")
    account_items = accounts.json()["accounts"]
    assert {item["login_name"] for item in account_items} == {"admin", "reader", "second"}
    assert {item["bound_user_id"] for item in account_items} == {None}
    assert {item["user_id"] for item in knowledge.json()["users"]} == {"department-a"}
    assert {item["workspace_id"] for item in knowledge.json()["workspaces"]} == {"workspace_existing"}


async def test_multiple_ordinary_accounts_can_bind_same_knowledge_domain(tmp_path) -> None:
    app, _metadata, policies, admin_token, _reader_token, admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post(
            "/api/v1/auth/register",
            json={"login_name": "second", "display_name": "第二用户", "password": "SecondSecure!123"},
        )
        assert registered.status_code == 201
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        accounts = await client.get("/api/v1/accounts")
        account_items = accounts.json()["accounts"]
        reader_id = next(item["account_id"] for item in account_items if item["login_name"] == "reader")
        second_id = next(item["account_id"] for item in account_items if item["login_name"] == "second")
        created_domain = await client.post(
            "/api/v1/users",
            json={"user_name": "第二知识域"},
        )
        assert created_domain.status_code == 201, created_domain.text
        second_domain_id = created_domain.json()["user_id"]

        first_binding = await client.put(
            f"/api/v1/accounts/{reader_id}/bindings",
            json={"user_ids": ["department-a", second_domain_id]},
        )
        second_binding = await client.put(
            f"/api/v1/accounts/{second_id}/binding",
            json={"user_id": "department-a"},
        )
        assert first_binding.status_code == 200, first_binding.text
        assert second_binding.status_code == 200, second_binding.text

        refreshed = (await client.get("/api/v1/accounts")).json()["accounts"]
        ordinary = {item["login_name"]: item for item in refreshed if item["permission_level"] < 1000}
        assert ordinary["reader"]["bound_user_ids"] == sorted(["department-a", second_domain_id])
        assert ordinary["second"]["bound_user_ids"] == ["department-a"]

        bindings = await policies.list_bindings("user", "department-a", USER_POLICY_ACTIONS)
        for account_id in (reader_id, second_id):
            for action in USER_POLICY_ACTIONS:
                matching = [
                    item
                    for item in bindings[action]
                    if item.principal_type == "account" and item.principal_id == account_id
                ]
                assert matching
                assert matching[0].effect == "allow"
                if account_id == second_id:
                    assert matching[0].immutable is True
                    assert matching[0].managed_by == "system.account_binding"
        second_domain_bindings = await policies.list_bindings("user", second_domain_id, USER_POLICY_ACTIONS)
        for action in USER_POLICY_ACTIONS:
            assert any(
                item.principal_type == "account"
                and item.principal_id == reader_id
                and item.immutable
                and item.managed_by == "system.account_binding"
                for item in second_domain_bindings[action]
            )

        rejected = await client.put(
            f"/api/v1/accounts/{admin_account_id}/binding",
            json={"user_id": "department-a"},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "superadmin_binding_unnecessary"

        removed_endpoint = await client.put(
            f"/api/v1/accounts/{admin_account_id}/responsibilities",
            json={"user_ids": []},
        )
        assert removed_endpoint.status_code == 404


async def test_accounts_are_separate_from_knowledge_users_and_binding_seeds_acl(tmp_path) -> None:
    app, metadata, policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created_account = await client.post(
            "/api/v1/auth/register",
            json={"login_name": "owner", "display_name": "负责人", "password": "OwnerSecure!123"},
        )
        assert created_account.status_code == 201
        assert {item.user_id for item in (await metadata.list_users()).users} == {"department-a"}

        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        created_user = await client.post(
            "/api/v1/users",
            json={
                "user_name": "新知识域",
                "read_min_level": 0,
                "workspace_create_min_level": 0,
            },
        )
        assert created_user.status_code == 201, created_user.text
        user_id = created_user.json()["user_id"]
        assert user_id.startswith("user_")
        accounts = (await client.get("/api/v1/accounts")).json()["accounts"]
        owner_id = next(item["account_id"] for item in accounts if item["login_name"] == "owner")

        bound = await client.put(
            f"/api/v1/accounts/{owner_id}/binding",
            json={"user_id": user_id},
        )
        assert bound.status_code == 200, bound.text
        assert bound.json() == {
            "account_id": owner_id,
            "bound_user_id": user_id,
            "bound_user_ids": [user_id],
        }
        owner_account = await client.get("/api/v1/accounts")
        owner = next(item for item in owner_account.json()["accounts"] if item["account_id"] == owner_id)
        assert owner["bound_user_ids"] == [user_id]
        bindings = await policies.list_bindings("user", user_id, USER_POLICY_ACTIONS)
        for action in USER_POLICY_ACTIONS:
            owner_binding = next(
                item for item in bindings[action] if item.principal_type == "account" and item.principal_id == owner_id
            )
            assert owner_binding.effect == "allow"
            assert owner_binding.immutable is True
            assert owner_binding.managed_by == "system.account_binding"


async def test_level_is_not_an_acl_principal_type(tmp_path) -> None:
    app, _metadata, _policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        rejected = await client.put(
            "/api/v1/users/department-a/bindings",
            json={
                "action": "webui.user.read",
                "bindings": [
                    {"principal_type": "level", "principal_id": "1", "effect": "allow"},
                ],
            },
        )
        assert rejected.status_code == 422


async def test_knowledge_mutations_and_resource_submissions_are_audited(tmp_path, monkeypatch) -> None:
    app, _metadata, _policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    monkeypatch.setattr(FileService, "submit_add", AsyncMock(return_value=TaskAccepted(task_id="a" * 32)))
    monkeypatch.setattr(
        FileService,
        "submit_delete_string",
        AsyncMock(return_value=TaskAccepted(task_id="b" * 32)),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        created = await client.post(
            "/api/v1/workspaces",
            json={"user_id": "department-a", "workspace_name": "审计知识库"},
        )
        workspace_id = created.json()["workspace_id"]
        assert (
            await client.patch(
                f"/api/v1/workspaces/{workspace_id}",
                json={"workspace_name": "审计知识库二"},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/api/v1/workspaces/{workspace_id}/policy",
                json={"read_min_level": 0, "cud_min_level": 1},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/api/v1/workspaces/{workspace_id}/bindings",
                json={"action": "webui.workspace.read", "bindings": []},
            )
        ).status_code == 200
        assert (
            await client.post(
                f"/api/v1/workspaces/{workspace_id}/resources",
                data={"type": "str", "content": "审计内容"},
            )
        ).status_code == 202
        assert (await client.delete(f"/api/v1/workspaces/{workspace_id}/strings/sha256:{'c' * 32}")).status_code == 202
        assert (
            await client.delete(
                f"/api/v1/workspaces/{workspace_id}",
                params={"confirm_name": "审计知识库二"},
            )
        ).status_code == 200
        audit = await client.get("/api/v1/audit-events", params={"limit": 500})
    actions = {event["action"] for event in audit.json()["events"] if event["resource_id"] == workspace_id}
    assert {
        "webui.workspace.create",
        "webui.workspace.rename",
        "webui.workspace.policy.update",
        "webui.workspace.bindings.update",
        "webui.resource.add.submit",
        "webui.workspace.delete",
    } <= actions


async def test_cleanup_failure_returns_cleanup_pending(tmp_path) -> None:
    app, _metadata, _policies, admin_token, _reader_token, _admin_account_id = await _setup(tmp_path)
    runtime = app.state.test_runtime
    runtime.elasticsearch.delete_workspace_contents = AsyncMock(side_effect=RuntimeError("ES unavailable"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", admin_token)
        deleted = await client.delete(
            "/api/v1/workspaces/workspace_existing",
            params={"confirm_name": "公开知识库"},
        )
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "cleanup_pending", "workspace_id": "workspace_existing"}
