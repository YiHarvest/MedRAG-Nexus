"""验证 Agent 确认、密码隔离和制品下载路由。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from medrag_nexus.agent import AgentStore, ArtifactService
from medrag_nexus.agent.router import create_agent_router
from medrag_nexus.identity import AccountStore, build_default_registry, create_account_router
from medrag_nexus.identity.security import PasswordService
from medrag_nexus.knowledge.policies import KnowledgePolicyStore


async def _setup(tmp_path):
    database_path = tmp_path / "metadata.sqlite3"
    accounts = AccountStore(database_path, build_default_registry())
    policies = KnowledgePolicyStore(database_path)
    actions = AgentStore(database_path)
    artifacts = ArtifactService(tmp_path / "agent-artifacts", actions)
    await accounts.ensure()
    await policies.ensure()
    await actions.ensure()
    admin = await accounts.bootstrap_superadmin(
        login_name="root",
        display_name="管理员",
        password_hash=PasswordService().hash("RootPassword!123"),
    )
    token, _ = await accounts.create_session(admin, timedelta(hours=1))
    runtime = SimpleNamespace(metadata=SimpleNamespace(), settings=SimpleNamespace(max_file_bytes=1024))
    app = FastAPI()
    app.include_router(create_account_router(accounts))
    app.include_router(create_agent_router(runtime, accounts, policies, actions, artifacts))
    return app, accounts, actions, artifacts, admin, token


async def test_confirm_then_secure_input_creates_account_without_persisting_password(tmp_path) -> None:
    app, accounts, actions, _artifacts, admin, token = await _setup(tmp_path)
    action = await actions.create_action(
        account_id=admin.account_id,
        conversation_id="conversation-1",
        tool_name="create_account",
        canonical_arguments={
            "login_name": "operator",
            "display_name": "操作员",
            "permission_level": 0,
            "group_keys": [],
            "must_change_password": True,
        },
        required_permissions=("webui.account.create",),
        risk_level="sensitive",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", token)
        confirmed = await client.post(f"/api/v1/agent/actions/{action.action_id}/confirm", json={})
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "confirmed"
        assert confirmed.json()["input"]["input_type"] == "password"

        completed = await client.post(
            f"/api/v1/agent/actions/{action.action_id}/input",
            json={"values": {"new_password": "OperatorPassword!123"}},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "succeeded"

    created = await accounts.get_account_by_login("operator")
    assert created is not None
    stored = await actions.get_action(action.action_id, account_id=admin.account_id)
    assert "OperatorPassword!123" not in str(stored.canonical_arguments)
    assert "OperatorPassword!123" not in str(stored.result_summary)
    assert "new_password" not in stored.canonical_arguments


async def test_change_password_requires_click_confirmation_before_secure_input(tmp_path) -> None:
    app, _accounts, actions, _artifacts, admin, token = await _setup(tmp_path)
    action = await actions.create_action(
        account_id=admin.account_id,
        conversation_id="conversation-1",
        tool_name="change_own_password",
        canonical_arguments={},
        required_permissions=("webui.account.password.change_self",),
        risk_level="sensitive",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", token)
        confirmed = await client.post(f"/api/v1/agent/actions/{action.action_id}/confirm", json={})

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["input"]["input_type"] == "password"
    assert (await actions.get_action(action.action_id, account_id=admin.account_id)).status == "confirmed"


async def test_artifact_link_allows_authorized_download_and_can_be_revoked(tmp_path) -> None:
    app, accounts, _actions, artifacts, admin, token = await _setup(tmp_path)
    artifact = await artifacts.create(
        owner_account_id=admin.account_id,
        file_name="回答.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=b"docx-bytes",
        required_permissions=("webui.agent.export",),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", token)
        downloaded = await client.get(f"/api/v1/agent/artifacts/{artifact.artifact_id}/download")
        assert downloaded.status_code == 200
        assert downloaded.content == b"docx-bytes"
        assert "private, no-store" in downloaded.headers["cache-control"]

        revoked = await client.delete(f"/api/v1/agent/artifacts/{artifact.artifact_id}")
        assert revoked.status_code == 204
        unavailable = await client.get(f"/api/v1/agent/artifacts/{artifact.artifact_id}/download")
        assert unavailable.status_code == 410

    assert await accounts.list_audit_events(limit=20, offset=0)


async def test_confirmation_rechecks_current_permission(tmp_path) -> None:
    app, _accounts, actions, _artifacts, admin, token = await _setup(tmp_path)
    action = await actions.create_action(
        account_id=admin.account_id,
        conversation_id="conversation-2",
        tool_name="delete_permission_group",
        canonical_arguments={"group_key": "webui.example"},
        required_permissions=("webui.permission.unknown",),
        risk_level="destructive",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", token)
        denied = await client.post(f"/api/v1/agent/actions/{action.action_id}/confirm", json={})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"
    stored = await actions.get_action(action.action_id, account_id=admin.account_id)
    assert stored.status == "pending"


async def test_repeated_confirmation_returns_current_terminal_status(tmp_path) -> None:
    app, _accounts, actions, _artifacts, admin, token = await _setup(tmp_path)
    action = await actions.create_action(
        account_id=admin.account_id,
        conversation_id="conversation-3",
        tool_name="update_account",
        canonical_arguments={"account_id": admin.account_id, "display_name": "新名称"},
        required_permissions=("webui.account.update",),
        risk_level="write",
    )
    await actions.confirm_action(action.action_id, account_id=admin.account_id)
    await actions.start_action(action.action_id, account_id=admin.account_id)
    await actions.succeed_action(
        action.action_id,
        account_id=admin.account_id,
        result_summary={"message": "操作已完成"},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("medrag_nexus_webui_account_session", token)
        repeated = await client.post(
            f"/api/v1/agent/actions/{action.action_id}/confirm",
            json={},
        )

    assert repeated.status_code == 200
    assert repeated.json()["status"] == "succeeded"
