from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from medrag_nexus.agent.context import AgentAuthorizationError, AgentContext
from medrag_nexus.agent.registry import AgentToolRegistry, ToolSpec, object_schema
from medrag_nexus.agent.tools import build_default_agent_tool_registry
from medrag_nexus.agent.tools import read as read_tools
from medrag_nexus.agent.tools.read import get_file_details, list_files, prepare_file_download
from medrag_nexus.identity.router import AccountPrincipal


def principal(*permissions: str, level: int = 0, enabled: bool = True) -> AccountPrincipal:
    account = SimpleNamespace(
        account_id="account-1",
        permission_level=level,
        enabled=enabled,
        must_change_password=False,
    )
    return AccountPrincipal(account=account, permissions=frozenset(permissions))


def context(initial: AccountPrincipal, resolver):
    return AgentContext(
        principal=initial,
        runtime=SimpleNamespace(),
        store=SimpleNamespace(),
        policies=SimpleNamespace(),
        resolve_principal=resolver,
        conversation_id="conversation-1",
    )


@pytest.mark.asyncio
async def test_registry_rechecks_permissions_immediately_before_execution():
    calls: list[str] = []

    async def handler(_context, _arguments):
        calls.append("executed")
        return {"ok": True}

    granted = principal("webui.workspace.rename")
    revoked = principal()
    current = granted

    async def resolve():
        return current

    registry = AgentToolRegistry(
        [
            ToolSpec(
                name="rename_workspace",
                description="rename",
                schema=object_schema(),
                handler=handler,
                required_permissions=("webui.workspace.rename",),
                risk_level="write",
            )
        ]
    )
    agent_context = context(granted, resolve)
    assert [tool.name for tool in registry.available_specs(agent_context)] == ["rename_workspace"]

    current = revoked
    with pytest.raises(AgentAuthorizationError, match="missing permission"):
        await registry.execute("rename_workspace", {}, agent_context)
    assert calls == []


@pytest.mark.asyncio
async def test_registry_refreshes_group_grant_without_restarting_conversation():
    denied = principal()
    granted = principal("webui.audit.read")
    current = denied

    async def resolve():
        return current

    async def handler(_context, _arguments):
        return "audit"

    registry = AgentToolRegistry([ToolSpec("list_audit", "audit", object_schema(), handler, ("webui.audit.read",))])
    agent_context = context(denied, resolve)
    assert registry.available_specs(agent_context) == ()
    current = granted
    assert await registry.refresh_available_tools(agent_context)
    assert await registry.execute("list_audit", {}, agent_context) == "audit"


@pytest.mark.asyncio
async def test_registry_records_permissions_and_resources_for_later_export():
    granted = principal("webui.audit.read", "webui.workspace.read")

    async def resolve():
        return granted

    async def handler(_context, _arguments):
        return {"citations": [{"workspace_id": "workspace-1"}]}

    registry = AgentToolRegistry([ToolSpec("read_audit", "读取审计", object_schema(), handler, ("webui.audit.read",))])
    agent_context = context(granted, resolve)

    await registry.execute("read_audit", {}, agent_context)

    assert agent_context.metadata["used_permissions"] == {"webui.audit.read"}
    assert (
        "workspace",
        "workspace-1",
        "webui.workspace.read",
    ) in agent_context.metadata["used_resources"]


def test_superadmin_guard_is_separate_from_permission_node():
    async def handler(_context, _arguments):  # pragma: no cover - discovery-only test
        return None

    registry = AgentToolRegistry(
        [
            ToolSpec(
                "create_account",
                "create",
                object_schema(),
                handler,
                ("webui.account.create",),
                superadmin_guard=True,
            )
        ]
    )
    member = context(principal("webui.account.create", level=2), lambda: None)
    admin = context(principal("webui.account.create", level=1000), lambda: None)
    assert registry.available_specs(member) == ()
    assert [tool.name for tool in registry.available_specs(admin)] == ["create_account"]


def test_default_registry_keeps_retrieval_and_hides_ungranted_management_tools():
    registry = build_default_agent_tool_registry()
    basic = principal(
        "webui.chat.use",
        "webui.retrieval.use",
        "webui.user.read",
        "webui.workspace.read",
        "webui.system.read",
        "webui.account.update_self",
        "webui.account.password.change_self",
        "webui.permission.catalog.read",
    )
    names = {tool.name for tool in registry.available_specs(context(basic, lambda: None))}
    assert {"retrieve_user_knowledge", "list_workspaces", "list_files", "get_file_details"} <= names
    assert {"create_workspace", "delete_workspace", "list_accounts", "update_account"}.isdisjoint(names)


@pytest.mark.asyncio
async def test_file_download_requires_same_turn_verified_file_identity():
    now = datetime.now(timezone.utc)
    workspace = SimpleNamespace(workspace_id="workspace-1", user_id="user-1")
    resource = SimpleNamespace(
        file_name="report.pdf",
        mime_type="application/pdf",
        content_hash="sha256-current",
        markdown_hash=None,
        size_bytes=123,
        parser="pdf",
        degraded=False,
        chunk_count=4,
        created_at=now,
        modified_at=now,
    )

    class Metadata:
        async def get_workspace(self, workspace_id):
            return workspace if workspace_id == workspace.workspace_id else None

        async def get_file(self, workspace_id, file_id):
            if (workspace_id, file_id) == (workspace.workspace_id, "file-real"):
                return resource
            return None

    class Policies:
        async def allows_user(self, *_args, **_kwargs):
            return True

        async def allows_workspace(self, *_args, **_kwargs):
            return True

    class CapabilityGateway:
        def __init__(self):
            self.calls = []

        async def invoke(self, capability, *, context, arguments):
            self.calls.append((capability, dict(arguments)))
            return {"status": "succeeded", "file_name": arguments["file_name"]}

    current = principal("webui.workspace.read", "webui.resource.file.download")

    async def resolve():
        return current

    gateway = CapabilityGateway()
    agent_context = AgentContext(
        principal=current,
        runtime=SimpleNamespace(metadata=Metadata()),
        store=SimpleNamespace(),
        policies=Policies(),
        resolve_principal=resolve,
        conversation_id="conversation-1",
        capability_gateway=gateway,
    )
    arguments = {"workspace_id": "workspace-1", "file_id": "file-real"}

    with pytest.raises(AgentAuthorizationError) as unverified:
        await prepare_file_download(agent_context, arguments)
    assert unverified.value.code == "file_verification_required"

    details = await get_file_details(agent_context, arguments)
    assert details["file_id"] == "file-real"
    assert details["downloadable"] is True
    result = await prepare_file_download(agent_context, arguments)

    assert result == {"status": "succeeded", "file_name": "report.pdf"}
    assert gateway.calls == [
        (
            "prepare_file_download",
            {"workspace_id": "workspace-1", "file_id": "file-real", "file_name": "report.pdf"},
        )
    ]


@pytest.mark.asyncio
async def test_file_download_accepts_identity_returned_by_same_turn_list_files(monkeypatch):
    workspace = SimpleNamespace(workspace_id="workspace-1", user_id="user-1")
    resource = SimpleNamespace(
        file_name="report.pdf",
        content_hash="sha256-current",
    )

    class Metadata:
        async def get_workspace(self, workspace_id):
            return workspace if workspace_id == workspace.workspace_id else None

        async def get_file(self, workspace_id, file_id):
            if (workspace_id, file_id) == (workspace.workspace_id, "file-real"):
                return resource
            return None

    class Policies:
        async def allows_user(self, *_args, **_kwargs):
            return True

        async def allows_workspace(self, *_args, **_kwargs):
            return True

    @dataclass
    class ListedFile:
        file_id: str
        file_name: str
        content_hash: str

    @dataclass
    class FileResponse:
        workspace_id: str
        files: list[ListedFile]

    class FileService:
        def __init__(self, _runtime):
            pass

        async def list_files(self, user_id, workspace_id, *, include_string_content=False):
            assert (user_id, workspace_id, include_string_content) == ("user-1", "workspace-1", False)
            return FileResponse(
                workspace_id=workspace_id,
                files=[ListedFile("file-real", resource.file_name, resource.content_hash)],
            )

    class CapabilityGateway:
        async def invoke(self, capability, *, context, arguments):
            assert capability == "prepare_file_download"
            return {"status": "succeeded", "file_name": arguments["file_name"]}

    current = principal("webui.workspace.read", "webui.resource.file.download")

    async def resolve():
        return current

    agent_context = AgentContext(
        principal=current,
        runtime=SimpleNamespace(metadata=Metadata()),
        store=SimpleNamespace(),
        policies=Policies(),
        resolve_principal=resolve,
        conversation_id="conversation-1",
        capability_gateway=CapabilityGateway(),
    )
    monkeypatch.setattr(read_tools, "FileService", FileService)

    listed = await list_files(agent_context, {"workspace_id": "workspace-1"})
    resource.content_hash = "sha256-changed"
    with pytest.raises(AgentAuthorizationError) as changed:
        await prepare_file_download(
            agent_context,
            {"workspace_id": "workspace-1", "file_id": "file-real"},
        )
    assert changed.value.code == "file_verification_required"

    await list_files(agent_context, {"workspace_id": "workspace-1"})
    result = await prepare_file_download(
        agent_context,
        {"workspace_id": "workspace-1", "file_id": "file-real"},
    )

    assert listed["files"][0]["file_id"] == "file-real"
    assert result == {"status": "succeeded", "file_name": "report.pdf"}


@pytest.mark.asyncio
async def test_list_files_recovers_invalid_workspace_id_from_visible_scope(monkeypatch):
    workspace = SimpleNamespace(workspace_id="workspace-real", workspace_name="论文库", user_id="user-1")
    resource = SimpleNamespace(file_name="manual.pdf", content_hash="sha256-manual")

    @dataclass
    class User:
        user_id: str
        user_name: str

    @dataclass
    class WorkspaceSummary:
        workspace_id: str
        workspace_name: str

    @dataclass
    class ListedFile:
        file_id: str
        file_name: str
        content_hash: str

    @dataclass
    class FileResponse:
        workspace_id: str
        files: list[ListedFile]

    class Metadata:
        async def get_workspace(self, workspace_id):
            return workspace if workspace_id == workspace.workspace_id else None

        async def get_file(self, workspace_id, file_id):
            if (workspace_id, file_id) == (workspace.workspace_id, "file-real"):
                return resource
            return None

        async def list_users(self):
            return SimpleNamespace(users=[User("user-1", "研究资料")])

        async def list_workspaces(self, user_id):
            assert user_id == "user-1"
            return SimpleNamespace(workspaces=[WorkspaceSummary(workspace.workspace_id, workspace.workspace_name)])

    class Policies:
        async def allows_user(self, *_args, **_kwargs):
            return True

        async def allows_workspace(self, *_args, **_kwargs):
            return True

    class FileService:
        def __init__(self, _runtime):
            pass

        async def list_files(self, user_id, workspace_id, *, include_string_content=False):
            assert (user_id, workspace_id, include_string_content) == ("user-1", "workspace-real", False)
            return FileResponse(
                workspace_id=workspace_id,
                files=[ListedFile("file-real", resource.file_name, resource.content_hash)],
            )

    class CapabilityGateway:
        async def invoke(self, capability, *, context, arguments):
            assert capability == "prepare_file_download"
            return {"status": "succeeded", "file_name": arguments["file_name"]}

    current = principal("webui.user.read", "webui.workspace.read", "webui.resource.file.download")

    async def resolve():
        return current

    agent_context = AgentContext(
        principal=current,
        runtime=SimpleNamespace(metadata=Metadata()),
        store=SimpleNamespace(),
        policies=Policies(),
        resolve_principal=resolve,
        conversation_id="conversation-1",
        capability_gateway=CapabilityGateway(),
    )
    monkeypatch.setattr(read_tools, "FileService", FileService)

    listed = await list_files(agent_context, {"workspace_id": "workspace-stale"})
    result = await prepare_file_download(
        agent_context,
        {"workspace_id": "workspace-real", "file_id": "file-real"},
    )

    assert listed["workspace_id_recovered"] is True
    assert listed["requested_workspace_id"] == "workspace-stale"
    assert listed["workspaces"][0]["workspace_id"] == "workspace-real"
    assert listed["workspaces"][0]["workspace_name"] == "论文库"
    assert result == {"status": "succeeded", "file_name": "manual.pdf"}


@pytest.mark.asyncio
async def test_create_knowledge_user_only_creates_click_confirmation():
    current = principal("webui.user.create", level=1000)

    async def resolve():
        return current

    agent_context = context(current, resolve)
    registry = build_default_agent_tool_registry()
    result = await registry.execute(
        "create_knowledge_user",
        {"user_name": "计算机域"},
        agent_context,
    )

    assert result == {
        "status": "confirmation_required",
        "tool_name": "create_knowledge_user",
        "arguments": {"user_name": "计算机域"},
        "required_permissions": ["webui.user.create"],
        "risk_level": "write",
        "confirmation_mode": "click",
    }
    spec = next(tool for tool in registry.specs if tool.name == "create_knowledge_user")
    assert spec.input_mode == "confirmation"


@pytest.mark.asyncio
async def test_destructive_tool_only_creates_confirmation_intent():
    @dataclass
    class Workspace:
        workspace_id: str = "workspace-1"
        user_id: str = "user-1"

    class Metadata:
        async def get_workspace(self, _workspace_id):
            return Workspace()

    class Policies:
        async def allows_user(self, *_args, **_kwargs):
            return True

        async def allows_workspace(self, *_args, **_kwargs):
            return True

    current = principal("webui.workspace.delete", "webui.user.read")

    async def resolve():
        return current

    agent_context = AgentContext(
        principal=current,
        runtime=SimpleNamespace(metadata=Metadata()),
        store=SimpleNamespace(),
        policies=Policies(),
        resolve_principal=resolve,
        conversation_id="conversation-1",
    )
    result = await build_default_agent_tool_registry().execute(
        "delete_workspace",
        {"workspace_id": "workspace-1", "workspace_name": "资料库"},
        agent_context,
    )
    assert result["status"] == "confirmation_required"
    assert result["confirmation_mode"] == "typed_text"
    assert result["arguments"]["workspace_name"] == "资料库"


@pytest.mark.asyncio
async def test_typed_confirmation_mode_and_target_name_reach_action_store():
    captured = {}

    class ActionStore:
        async def create_action(self, **kwargs):
            captured.update(kwargs)
            return {"action_id": "action-1"}

    class Metadata:
        async def get_workspace(self, _workspace_id):
            return SimpleNamespace(workspace_id="workspace-1", user_id="user-1")

    class Policies:
        async def allows_user(self, *_args, **_kwargs):
            return True

        async def allows_workspace(self, *_args, **_kwargs):
            return True

    current = principal("webui.workspace.delete", "webui.user.read")

    async def resolve():
        return current

    agent_context = AgentContext(
        principal=current,
        runtime=SimpleNamespace(metadata=Metadata()),
        store=SimpleNamespace(),
        policies=Policies(),
        resolve_principal=resolve,
        conversation_id="conversation-1",
        action_store=ActionStore(),
    )
    await build_default_agent_tool_registry().execute(
        "delete_workspace",
        {"workspace_id": "workspace-1", "workspace_name": "资料库"},
        agent_context,
    )
    assert captured["confirmation_mode"] == "typed_text"
    assert captured["target"] == {
        "resource_type": "workspace",
        "resource_id": "workspace-1",
        "display_name": "资料库",
    }


def test_password_tools_never_accept_password_in_model_schema():
    registry = build_default_agent_tool_registry()
    password_tools = {
        tool.name: tool for tool in registry.specs if tool.name in {"change_own_password", "reset_account_password"}
    }
    assert password_tools["change_own_password"].schema["properties"] == {}
    reset_properties = password_tools["reset_account_password"].schema["properties"]
    assert "new_password" not in reset_properties
    assert "current_password" not in reset_properties
    assert password_tools["change_own_password"].input_mode == "secure_form"
    assert password_tools["reset_account_password"].input_mode == "secure_form"
