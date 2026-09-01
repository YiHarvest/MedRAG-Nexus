"""Tests for durable, account-bound WebUI Agent action intents."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from medrag_nexus.webui.agent import (
    ActionOwnershipError,
    ActionPayloadError,
    ActionStateError,
    ActionTarget,
    AgentStore,
    IdempotencyConflictError,
    InvalidConfirmationError,
)
from medrag_nexus.webui.agent.service import ConfirmedActionExecutor
from medrag_nexus.webui.agent.tools import build_default_agent_tool_registry


async def test_action_lifecycle_is_account_bound_and_idempotent(tmp_path) -> None:
    store = AgentStore(tmp_path / "webui.sqlite3")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    action = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="delete_workspace",
        canonical_arguments={"workspace_id": "workspace-a", "confirm_name": "资料库"},
        required_permissions=("webui.workspace.delete", "webui.workspace.read"),
        target=ActionTarget(resource_type="workspace", resource_id="workspace-a", version="7", display_name="资料库"),
        risk_level="destructive",
        confirmation_mode="typed_text",
        idempotency_key="request-1",
        now=now,
    )

    duplicate = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="delete_workspace",
        canonical_arguments={"confirm_name": "资料库", "workspace_id": "workspace-a"},
        required_permissions=("webui.workspace.read", "webui.workspace.delete"),
        target={
            "resource_type": "workspace",
            "resource_id": "workspace-a",
            "version": "7",
            "display_name": "资料库",
        },
        risk_level="destructive",
        confirmation_mode="typed_text",
        idempotency_key="request-1",
        now=now + timedelta(seconds=1),
    )
    assert duplicate.action_id == action.action_id
    assert duplicate.canonical_arguments == {
        "confirm_name": "资料库",
        "workspace_id": "workspace-a",
    }

    with pytest.raises(ActionOwnershipError):
        await store.confirm_action(action.action_id, account_id="account-b", now=now)

    with pytest.raises(InvalidConfirmationError):
        await store.confirm_action(action.action_id, account_id="account-a", confirmation_text="错误名称", now=now)
    confirmed = await store.confirm_action(
        action.action_id, account_id="account-a", confirmation_text="资料库", now=now
    )
    assert confirmed.status == "confirmed"
    assert (
        await store.confirm_action(action.action_id, account_id="account-a", confirmation_text="资料库", now=now)
    ).status == "confirmed"
    executing = await store.start_action(action.action_id, account_id="account-a", now=now)
    assert executing.status == "executing"
    succeeded = await store.succeed_action(
        action.action_id,
        account_id="account-a",
        result_summary={"deleted": True},
        now=now + timedelta(seconds=2),
    )
    assert succeeded.status == "succeeded"
    assert succeeded.result_summary == {"deleted": True}
    assert (
        await store.succeed_action(
            action.action_id,
            account_id="account-a",
            result_summary={"this_second_result_is_ignored": True},
            now=now + timedelta(seconds=3),
        )
    ).result_summary == {"deleted": True}
    with pytest.raises(ActionStateError):
        await store.cancel_action(action.action_id, account_id="account-a", now=now)


async def test_action_expiry_does_not_interrupt_executing_work(tmp_path) -> None:
    store = AgentStore(tmp_path / "webui.sqlite3")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    pending = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="reset_password",
        canonical_arguments={"account_id": "target-account"},
        ttl=timedelta(minutes=1),
        now=now,
    )
    assert (
        await store.get_action(pending.action_id, account_id="account-a", now=now + timedelta(minutes=2))
    ).status == "expired"
    with pytest.raises(ActionStateError):
        await store.confirm_action(pending.action_id, account_id="account-a", now=now + timedelta(minutes=2))

    submitted = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="delete_file",
        canonical_arguments={"file_id": "file-a"},
        ttl=timedelta(minutes=1),
        now=now,
    )
    await store.confirm_action(submitted.action_id, account_id="account-a", now=now)
    await store.start_action(submitted.action_id, account_id="account-a", now=now)
    assert await store.expire_due_actions(now=now + timedelta(days=1)) == 0
    assert (
        await store.get_action(submitted.action_id, account_id="account-a", now=now + timedelta(days=1))
    ).status == "executing"


async def test_action_arguments_reject_secrets_binary_and_idempotency_reuse(tmp_path) -> None:
    store = AgentStore(tmp_path / "webui.sqlite3")
    common = {
        "account_id": "account-a",
        "conversation_id": "conversation-1",
        "tool_name": "update_account",
    }
    with pytest.raises(ActionPayloadError):
        await store.create_action(**common, canonical_arguments={"new_password": "must-not-persist"})
    with pytest.raises(ActionPayloadError):
        await store.create_action(**common, canonical_arguments={"upload": {"file_bytes": b"raw"}})

    created = await store.create_action(
        **common,
        canonical_arguments={"display_name": "新名称"},
        idempotency_key="same-request",
    )
    assert created.status == "pending"
    with pytest.raises(IdempotencyConflictError):
        await store.create_action(
            **common,
            canonical_arguments={"display_name": "另一个名称"},
            idempotency_key="same-request",
        )

    with sqlite3.connect(tmp_path / "webui.sqlite3") as database:
        persisted = database.execute(
            "SELECT canonical_arguments_json FROM webui_agent_actions WHERE action_id = ?", (created.action_id,)
        ).fetchone()[0]
    assert "新名称" in persisted
    assert "password" not in persisted


async def test_action_result_allows_numeric_byte_count_but_rejects_binary_content(tmp_path) -> None:
    store = AgentStore(tmp_path / "webui.sqlite3")
    action = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="create_knowledge_user",
        canonical_arguments={"user_id": "tech", "user_name": "技术域"},
    )
    await store.confirm_action(action.action_id, account_id="account-a")
    await store.start_action(action.action_id, account_id="account-a")

    succeeded = await store.succeed_action(
        action.action_id,
        account_id="account-a",
        result_summary={"total_size_bytes": 0, "user_name": "技术域"},
    )
    assert succeeded.status == "succeeded"
    assert succeeded.result_summary == {"total_size_bytes": 0, "user_name": "技术域"}

    with pytest.raises(ActionPayloadError):
        AgentStore._safe_summary({"raw_bytes": "不允许持久化的内容"})


async def test_completed_action_cleanup_preserves_active_work(tmp_path) -> None:
    store = AgentStore(tmp_path / "webui.sqlite3")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    completed = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="rename_workspace",
        canonical_arguments={"name": "new"},
        now=now - timedelta(days=40),
        ttl=timedelta(days=100),
    )
    await store.confirm_action(completed.action_id, account_id="account-a", now=now - timedelta(days=40))
    await store.start_action(completed.action_id, account_id="account-a", now=now - timedelta(days=40))
    await store.succeed_action(completed.action_id, account_id="account-a", now=now - timedelta(days=40))
    active = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="delete_file",
        canonical_arguments={"file_id": "file-a"},
        now=now - timedelta(days=40),
        ttl=timedelta(days=100),
    )
    await store.confirm_action(active.action_id, account_id="account-a", now=now - timedelta(days=40))
    await store.start_action(active.action_id, account_id="account-a", now=now - timedelta(days=40))

    assert await store.cleanup_completed_actions(now=now) == 1
    actions = await store.list_actions(account_id="account-a", now=now)
    assert [action.action_id for action in actions] == [active.action_id]


async def test_confirmed_create_actions_reuse_webui_api_routes(tmp_path) -> None:
    store = AgentStore(tmp_path / "webui.sqlite3")
    executor = ConfirmedActionExecutor()
    create_user = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="create_knowledge_user",
        canonical_arguments={"user_id": "computer-domain", "user_name": "计算机域"},
        required_permissions=("webui.user.create",),
        risk_level="write",
    )
    create_workspace = await store.create_action(
        account_id="account-a",
        conversation_id="conversation-1",
        tool_name="create_workspace",
        canonical_arguments={"user_id": "computer-domain", "workspace_name": "技术库"},
        required_permissions=("webui.workspace.create",),
        risk_level="write",
    )

    assert executor._request_spec(create_user, None) == (
        "POST",
        "/api/v1/users",
        {"user_id": "computer-domain", "user_name": "计算机域"},
    )
    assert executor._request_spec(create_workspace, None) == (
        "POST",
        "/api/v1/workspaces",
        {"user_id": "computer-domain", "workspace_name": "技术库"},
    )


async def test_every_interactive_tool_has_a_confirmed_webui_request(tmp_path) -> None:
    store = AgentStore(tmp_path / "webui.sqlite3")
    executor = ConfirmedActionExecutor()
    arguments = {
        "create_knowledge_user": {"user_id": "domain-1", "user_name": "知识域一"},
        "rename_knowledge_user": {"user_id": "domain-1", "user_name": "新知识域"},
        "create_workspace": {"user_id": "domain-1", "workspace_name": "知识库一"},
        "rename_workspace": {"workspace_id": "workspace-1", "workspace_name": "新知识库"},
        "add_text_resource": {"workspace_id": "workspace-1", "content": "测试内容"},
        "request_file_upload": {"workspace_id": "workspace-1"},
        "delete_file": {"workspace_id": "workspace-1", "file_id": "file-1", "file_name": "资料.pdf"},
        "delete_text_resource": {"workspace_id": "workspace-1", "content_hash": "abc123"},
        "delete_workspace": {"workspace_id": "workspace-1", "workspace_name": "资料库"},
        "delete_knowledge_user": {"user_id": "domain-1", "user_name": "知识域一"},
        "change_own_password": {},
        "revoke_artifact": {"artifact_id": "artifact-1"},
        "create_account": {"login_name": "operator", "display_name": "操作员"},
        "update_account": {"account_id": "account-2", "display_name": "新名称"},
        "reset_account_password": {"account_id": "account-2"},
        "bind_account_to_user": {"account_id": "account-2", "user_id": "domain-1"},
        "create_permission_group": {"group_key": "group-1", "permissions": []},
        "update_permission_group": {"group_key": "group-1", "description": "更新"},
        "delete_permission_group": {"group_key": "group-1"},
        "leave_own_permission_group": {"group_key": "group-1"},
        "update_user_policy": {
            "user_id": "domain-1",
            "read_min_level": 0,
            "workspace_create_min_level": 0,
        },
        "update_workspace_policy": {
            "workspace_id": "workspace-1",
            "read_min_level": 0,
            "cud_min_level": 0,
        },
        "replace_user_bindings": {"user_id": "domain-1", "action": "webui.user.read", "bindings": []},
        "replace_workspace_bindings": {
            "workspace_id": "workspace-1",
            "action": "webui.workspace.read",
            "bindings": [],
        },
    }
    interactive_specs = [
        spec for spec in build_default_agent_tool_registry().specs if spec.input_mode != "model"
    ]

    assert {spec.name for spec in interactive_specs} == set(arguments)
    requests = {}
    for spec in interactive_specs:
        action = await store.create_action(
            account_id="account-a",
            conversation_id="conversation-1",
            tool_name=spec.name,
            canonical_arguments=arguments[spec.name],
            required_permissions=spec.required_permissions,
            risk_level=spec.risk_level,
            confirmation_mode="typed_text" if spec.name in {"delete_workspace", "delete_knowledge_user"} else "click",
        )
        requests[spec.name] = executor._request_spec(
            action,
            {"current_password": "old-password", "new_password": "new-password"},
        )

    assert requests["add_text_resource"] == (
        "POST",
        "/api/v1/workspaces/workspace-1/resources",
        {"type": "str", "content": "测试内容"},
    )
    assert "confirm_name=%E8%B5%84%E6%96%99%E5%BA%93" in requests["delete_workspace"][1]
    assert "confirm_name=%E7%9F%A5%E8%AF%86%E5%9F%9F%E4%B8%80" in requests["delete_knowledge_user"][1]
