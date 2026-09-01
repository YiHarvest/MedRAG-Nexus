"""验证 WebUI 独立身份、Session、权限和审计旁路。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from jd_knowledge.webui import WebUiStore, build_default_registry, create_webui_router
from jd_knowledge.webui.permissions import PermissionEngine, PermissionRegistry, PluginManifest
from jd_knowledge.webui.security import PasswordService


def _app(store: WebUiStore) -> FastAPI:
    app = FastAPI()
    app.include_router(create_webui_router(store))
    return app


async def _bootstrap_admin(store: WebUiStore, password: str = "SuperSecure!123") -> None:
    encoded = PasswordService().hash(password)
    await store.bootstrap_superadmin(login_name="root", display_name="Root Admin", password_hash=encoded)


async def test_registration_only_creates_unbound_account_and_hashed_session(tmp_path: Path) -> None:
    path = tmp_path / "agenthub.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, user_name TEXT NOT NULL, "
            "resource_count INTEGER NOT NULL DEFAULT 0, file_count INTEGER NOT NULL DEFAULT 0, "
            "str_count INTEGER NOT NULL DEFAULT 0, total_size_bytes INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, modified_at TEXT NOT NULL)"
        )
    store = WebUiStore(path, build_default_registry())
    app = _app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/webui/v1/auth/register",
            json={"login_name": "alice", "display_name": "Alice", "password": "AliceSecure!123"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["account"]["bound_user_id"] is None
        assert body["account"]["permission_level"] == 0
        assert body["account"]["groups"] == []
        assert "webui.retrieval.use" in body["permissions"]
        assert "webui.system.read" in body["permissions"]
        assert "webui.permission.catalog.read" in body["permissions"]
        assert body["expires_at"] is not None
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie

        me = await client.get("/api/webui/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["account"]["login_name"] == "alice"
        assert me.json()["permissions"] == body["permissions"]
        assert (await client.get("/api/webui/v1/permission-catalog")).status_code == 200

    with sqlite3.connect(path) as db:
        password_hash = db.execute("SELECT password_hash FROM webui_accounts").fetchone()[0]
        session_hash = db.execute("SELECT session_id_hash FROM webui_sessions").fetchone()[0]
        knowledge_user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert password_hash.startswith("$argon2id$")
    assert "AliceSecure!123" not in password_hash
    assert len(session_hash) == 64
    assert "jd_webui_session=" not in session_hash
    assert knowledge_user_count == 0


async def test_registered_user_cannot_access_admin_accounts(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test") as client:
        await client.post(
            "/api/webui/v1/auth/register",
            json={"login_name": "reader", "display_name": "Reader", "password": "ReaderSecure!123"},
        )
        response = await client.get("/api/webui/v1/accounts")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"


async def test_webui_password_accepts_three_characters_and_rejects_two(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test") as client:
        accepted = await client.post(
            "/api/webui/v1/auth/register",
            json={"login_name": "mini", "display_name": "短密码用户", "password": "a3!"},
        )
        rejected = await client.post(
            "/api/webui/v1/auth/register",
            json={"login_name": "tiny", "display_name": "过短密码用户", "password": "a!"},
        )
    assert accepted.status_code == 201
    assert rejected.status_code == 422


async def test_superadmin_can_create_peer_and_all_superadmins_are_immutable(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    await _bootstrap_admin(store)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test") as client:
        login = await client.post(
            "/api/webui/v1/auth/login", json={"login_name": "root", "password": "SuperSecure!123"}
        )
        assert login.status_code == 200
        assert "webui.account.create_superadmin" in login.json()["permissions"]

        created = await client.post(
            "/api/webui/v1/accounts",
            json={
                "login_name": "root2",
                "display_name": "Second Root",
                "password": "SecondSecure!123",
                "permission_level": 1000,
                "group_keys": [],
            },
        )
        assert created.status_code == 201
        assert created.json()["bound_user_id"] is None
        assert created.json()["capabilities"]["protected"] is True
        assert "webui.workspace.delete" in created.json()["permissions"]
        second_id = created.json()["account_id"]
        disabled = await client.patch(f"/api/webui/v1/accounts/{second_id}", json={"enabled": False})
        assert disabled.status_code == 409
        assert disabled.json()["detail"]["code"] == "superadmin_immutable"

        root_id = login.json()["account"]["account_id"]
        rejected = await client.patch(f"/api/webui/v1/accounts/{root_id}", json={"enabled": False})
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "superadmin_immutable"
        reset = await client.post(
            f"/api/webui/v1/accounts/{second_id}/password/reset",
            json={"new_password": "NoResetAllowed!123"},
        )
        revoke = await client.post(f"/api/webui/v1/accounts/{second_id}/sessions/revoke")
        assert reset.status_code == 409
        assert revoke.status_code == 404


async def test_password_change_revokes_old_session_and_new_password_logs_in(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    app = _app(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/api/webui/v1/auth/register",
            json={"login_name": "change", "display_name": "Change", "password": "OriginalSecure!123"},
        )
        changed = await client.post(
            "/api/webui/v1/account/password",
            json={"current_password": "OriginalSecure!123", "new_password": "Replacement!456"},
        )
        assert changed.status_code == 200
        assert (await client.get("/api/webui/v1/auth/me")).status_code == 401
        old_login = await client.post(
            "/api/webui/v1/auth/login", json={"login_name": "change", "password": "OriginalSecure!123"}
        )
        assert old_login.status_code == 401
        new_login = await client.post(
            "/api/webui/v1/auth/login", json={"login_name": "change", "password": "Replacement!456"}
        )
        assert new_login.status_code == 200


async def test_login_lock_cannot_be_cleared_by_another_failed_attempt(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    await _bootstrap_admin(store)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test") as client:
        for _ in range(5):
            response = await client.post(
                "/api/webui/v1/auth/login",
                json={"login_name": "root", "password": "wrong-password"},
            )
            assert response.status_code == 401

        locked = await client.post(
            "/api/webui/v1/auth/login",
            json={"login_name": "root", "password": "SuperSecure!123"},
        )
        assert locked.status_code == 423
        still_locked = await client.post(
            "/api/webui/v1/auth/login",
            json={"login_name": "root", "password": "wrong-password"},
        )
        assert still_locked.status_code == 423

    account = await store.get_account_by_login("root")
    assert account is not None
    assert account.failed_login_count == 5
    assert account.locked_until is not None


async def test_concurrent_failed_logins_atomically_reach_locked_state(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    await _bootstrap_admin(store)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test") as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/webui/v1/auth/login",
                    json={"login_name": "root", "password": "wrong-password"},
                )
                for _ in range(20)
            )
        )
        assert {response.status_code for response in responses} <= {401, 423}
        assert any(response.status_code == 401 for response in responses)
        locked = await client.post(
            "/api/webui/v1/auth/login",
            json={"login_name": "root", "password": "SuperSecure!123"},
        )
        assert locked.status_code == 423

    account = await store.get_account_by_login("root")
    assert account is not None
    assert account.failed_login_count == 5
    assert account.locked_until is not None


async def test_only_registered_levels_can_be_assigned(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    await _bootstrap_admin(store)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test") as client:
        assert (
            await client.post(
                "/api/webui/v1/auth/login",
                json={"login_name": "root", "password": "SuperSecure!123"},
            )
        ).status_code == 200
        created = await client.post(
            "/api/webui/v1/accounts",
            json={
                "login_name": "invalidlevel",
                "display_name": "Invalid Level",
                "password": "InvalidLevel!123",
                "permission_level": 3,
                "group_keys": [],
            },
        )
    assert created.status_code == 422
    assert created.json()["detail"]["code"] == "invalid_permission_level"


async def test_retired_level_and_session_permission_are_migrated(tmp_path: Path) -> None:
    path = tmp_path / "metadata.sqlite3"
    registry = build_default_registry()
    store = WebUiStore(path, registry)
    await _bootstrap_admin(store)
    member = await store.create_registered_account(
        login_name="legacy-level",
        display_name="Legacy Level",
        password_hash=PasswordService().hash("LegacySecure!123"),
    )
    now = "2026-08-29T00:00:00+00:00"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE webui_accounts SET permission_level = 3 WHERE account_id = ?", (member.account_id,))
        db.execute(
            "INSERT INTO webui_user_policies"
            "(user_id, read_min_level, workspace_create_min_level, policy_version, modified_at) "
            "VALUES ('legacy-user', 3, 3, 1, ?)",
            (now,),
        )
        db.execute(
            "INSERT INTO webui_workspace_policies"
            "(workspace_id, read_min_level, cud_min_level, policy_version, created_at, modified_at) "
            "VALUES ('legacy-workspace', 3, 3, 1, ?, ?)",
            (now, now),
        )
        db.execute(
            "INSERT INTO webui_permission_groups"
            "(group_key, name, description, system_managed, modified_at) "
            "VALUES ('webui.registered', '旧默认角色', '旧默认角色', 1, ?)",
            (now,),
        )
        db.execute(
            "INSERT INTO webui_permission_nodes"
            "(permission_key, description, plugin_id, available, custom_assignable, modified_at) "
            "VALUES ('webui.account.sessions.revoke', 'legacy', 'webui.core_accounts', 1, 1, ?)",
            (now,),
        )
        db.execute(
            "INSERT INTO webui_group_permissions(group_key, permission_key) "
            "VALUES ('webui.registered', 'webui.account.sessions.revoke')"
        )
        db.execute(
            "INSERT INTO webui_account_groups(account_id, group_key) VALUES (?, 'webui.registered')",
            (member.account_id,),
        )

    migrated = WebUiStore(path, registry)
    await migrated.ensure()
    migrated_member = await migrated.get_account(member.account_id)
    assert migrated_member is not None
    assert migrated_member.permission_level == 2
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT read_min_level, workspace_create_min_level FROM webui_user_policies "
            "WHERE user_id = 'legacy-user'"
        ).fetchone() == (2, 2)
        assert db.execute(
            "SELECT read_min_level, cud_min_level FROM webui_workspace_policies "
            "WHERE workspace_id = 'legacy-workspace'"
        ).fetchone() == (2, 2)
        assert db.execute(
            "SELECT 1 FROM webui_permission_nodes WHERE permission_key = 'webui.account.sessions.revoke'"
        ).fetchone() is None
        assert db.execute("SELECT 1 FROM webui_permission_levels WHERE level = 3").fetchone() is None
        assert db.execute(
            "SELECT 1 FROM webui_audit_events WHERE action = 'webui.permission_model.migrate'"
        ).fetchone() is not None
        assert db.execute(
            "SELECT 1 FROM webui_permission_groups WHERE group_key = 'webui.registered'"
        ).fetchone() is None
    assert (await migrated.get_account(member.account_id)).groups == []


async def test_superadmin_resets_password_and_reads_audit_events(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    await _bootstrap_admin(store)
    target = await store.create_registered_account(
        login_name="resetme",
        display_name="Reset Me",
        password_hash=PasswordService().hash("OriginalSecure!123"),
    )
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test") as client:
        await client.post(
            "/api/webui/v1/auth/login",
            json={"login_name": "root", "password": "SuperSecure!123"},
        )
        reset = await client.post(
            f"/api/webui/v1/accounts/{target.account_id}/password/reset",
            json={"new_password": "ReplacementSecure!456", "must_change_password": False},
        )
        assert reset.status_code == 200
        audit = await client.get("/api/webui/v1/audit-events")
        assert audit.status_code == 200
        actions = {event["action"] for event in audit.json()["events"]}
        assert "webui.account.password.reset" in actions

        await client.post("/api/webui/v1/auth/logout")
        new_login = await client.post(
            "/api/webui/v1/auth/login",
            json={"login_name": "resetme", "password": "ReplacementSecure!456"},
        )
        assert new_login.status_code == 200


async def test_store_allows_multiple_accounts_to_share_bound_user_id(tmp_path: Path) -> None:
    path = tmp_path / "metadata.sqlite3"
    registry = build_default_registry()
    store = WebUiStore(path, registry)
    await _bootstrap_admin(store)
    second = await store.create_registered_account(
        login_name="legacy",
        display_name="Legacy User",
        password_hash=PasswordService().hash("LegacySecure!123"),
    )
    third = await store.create_registered_account(
        login_name="legacy-second",
        display_name="Legacy Second User",
        password_hash=PasswordService().hash("LegacySecure!456"),
    )
    root = await store.get_account_by_login("root")
    assert root is not None
    with sqlite3.connect(path) as db:
        now = "2026-08-29T00:00:00+00:00"
        db.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, user_name TEXT NOT NULL, created_at TEXT NOT NULL, "
            "modified_at TEXT NOT NULL)"
        )
        db.execute("INSERT INTO users VALUES ('legacy-user', 'Legacy', ?, ?)", (now, now))
        db.execute(
            "UPDATE webui_accounts SET bound_user_id = ? WHERE account_id = ?",
            ("legacy-user", second.account_id),
        )
        db.execute(
            "UPDATE webui_accounts SET bound_user_id = ? WHERE account_id = ?",
            ("legacy-user", third.account_id),
        )
        db.execute("UPDATE webui_accounts SET bound_user_id = 'legacy-user' WHERE account_id = ?", (root.account_id,))

    migrated_store = WebUiStore(path, registry)
    await migrated_store.ensure()
    migrated_second = await migrated_store.get_account(second.account_id)
    migrated_third = await migrated_store.get_account(third.account_id)
    migrated_root = await migrated_store.get_account(root.account_id)
    assert migrated_second is not None
    assert migrated_second.bound_user_id == "legacy-user"
    assert migrated_third is not None
    assert migrated_third.bound_user_id == "legacy-user"
    assert migrated_root is not None
    assert migrated_root.bound_user_id is None
    with sqlite3.connect(path) as db:
        index = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_webui_accounts_bound_user'"
        ).fetchone()
    assert index is None


async def test_store_ensure_is_idempotent_and_registry_rejects_bad_plugins(tmp_path: Path) -> None:
    registry = build_default_registry()
    store = WebUiStore(tmp_path / "metadata.sqlite3", registry)
    await store.ensure()
    await store.ensure()
    with sqlite3.connect(store.path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        nodes = db.execute("SELECT COUNT(*) FROM webui_permission_nodes").fetchone()[0]
    assert {
        "webui_accounts",
        "webui_sessions",
        "webui_permission_nodes",
        "webui_permission_groups",
        "webui_group_permissions",
        "webui_account_groups",
        "webui_account_user_bindings",
        "webui_user_policies",
        "webui_workspace_policies",
        "webui_policy_bindings",
        "webui_audit_events",
    } <= tables
    assert nodes == len(registry.nodes)

    invalid = PermissionRegistry()
    try:
        invalid.register_node("outside.permission", "bad", plugin_id="bad")
    except ValueError as exc:
        assert "webui namespace" in str(exc)
    else:  # pragma: no cover - 断言保护
        raise AssertionError("non-WebUI permission namespace was accepted")


def test_permission_engine_combines_node_level_and_explicit_deny() -> None:
    engine = PermissionEngine(build_default_registry())
    permissions = {"webui.workspace.read"}
    assert not engine.allows_resource(
        "webui.workspace.read", permissions, account_level=2, minimum_level=2, acl_effect=None
    )
    assert engine.allows_resource(
        "webui.workspace.read", permissions, account_level=2, minimum_level=2, acl_effect="allow"
    )
    assert not engine.allows_resource(
        "webui.workspace.read", permissions, account_level=1, minimum_level=2, acl_effect="allow"
    )
    assert not engine.allows_resource(
        "webui.workspace.read", permissions, account_level=2, minimum_level=2, acl_effect="deny"
    )


async def test_dynamic_permission_group_crud_and_multi_group_union(tmp_path: Path) -> None:
    store = WebUiStore(tmp_path / "metadata.sqlite3", build_default_registry())
    await _bootstrap_admin(store)
    async with AsyncClient(transport=ASGITransport(app=_app(store)), base_url="http://test") as client:
        await client.post(
            "/api/webui/v1/auth/login", json={"login_name": "root", "password": "SuperSecure!123"}
        )
        catalog = await client.get("/api/webui/v1/permission-catalog")
        assert catalog.status_code == 200
        assert [item["value"] for item in catalog.json()["levels"]] == [0, 1, 2, 1000]
        assert catalog.json()["groups"] == []
        assert "webui.workspace.read" in catalog.json()["levels"][0]["permissions"]
        assert {item["plugin_id"] for item in catalog.json()["plugins"]} == {
            "webui.core_accounts",
            "webui.knowledge",
        }

        created_group = await client.post(
            "/api/webui/v1/permission-groups",
            json={
                "group_key": "webui.custom.domain_creator",
                "name": "知识域创建者",
                "description": "",
                "permissions": ["webui.user.create"],
            },
        )
        assert created_group.status_code == 201
        assert created_group.json()["name"] == "知识域创建者"
        assert created_group.json()["description"] == ""
        second_group = await client.post(
            "/api/webui/v1/permission-groups",
            json={
                "group_key": "webui.custom.policy_reader",
                "name": "审计查看组",
                "description": "",
                "permissions": ["webui.audit.read"],
            },
        )
        assert second_group.status_code == 201
        account = await client.post(
            "/api/webui/v1/accounts",
            json={
                "login_name": "combined",
                "display_name": "Combined",
                "password": "Combined!123",
                "permission_level": 1,
                "group_keys": ["webui.custom.domain_creator", "webui.custom.policy_reader"],
            },
        )
        assert account.status_code == 201
        account_id = account.json()["account_id"]
        assert {
            "webui.user.create",
            "webui.audit.read",
            "webui.workspace.read",
        } <= await store.permission_keys(account_id)

        patched = await client.patch(
            "/api/webui/v1/permission-groups/webui.custom.domain_creator",
            json={"name": "知识域策略员", "permissions": ["webui.user.policy.manage"]},
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "知识域策略员"
        assert patched.json()["permissions"] == ["webui.user.policy.manage"]
        delete_assigned = await client.delete("/api/webui/v1/permission-groups/webui.custom.domain_creator")
        assert delete_assigned.status_code == 409

        await client.post(
            "/api/webui/v1/auth/login",
            json={"login_name": "combined", "password": "Combined!123"},
        )
        left = await client.delete(
            "/api/webui/v1/account/permission-groups/webui.custom.policy_reader"
        )
        assert left.status_code == 200, left.text
        assert left.json()["groups"] == ["webui.custom.domain_creator"]
        assert "webui.audit.read" not in left.json()["permissions"]
        assert "webui.audit.read" not in await store.permission_keys(account_id)


async def test_binding_allows_multiple_accounts_per_domain_and_does_not_create_users(tmp_path: Path) -> None:
    path = tmp_path / "metadata.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, user_name TEXT NOT NULL, created_at TEXT NOT NULL, "
            "modified_at TEXT NOT NULL)"
        )
        db.execute("INSERT INTO users VALUES ('legal', 'Legal', 'now', 'now')")
        db.execute("INSERT INTO users VALUES ('finance', 'Finance', 'now', 'now')")
    store = WebUiStore(path, build_default_registry())
    await _bootstrap_admin(store)
    first = await store.create_registered_account(
        login_name="first", display_name="First", password_hash=PasswordService().hash("First!123")
    )
    second = await store.create_registered_account(
        login_name="second", display_name="Second", password_hash=PasswordService().hash("Second!123")
    )
    bound = await store.bind_account_user(first.account_id, "legal", actor_account_id=first.account_id)
    assert bound.bound_user_id == "legal"
    multiple = await store.set_account_user_bindings(
        first.account_id,
        ["legal", "finance"],
        actor_account_id=first.account_id,
    )
    assert multiple.bound_user_ids == ["finance", "legal"]
    second_bound = await store.bind_account_user(
        second.account_id, "legal", actor_account_id=second.account_id
    )
    assert second_bound.bound_user_id == "legal"
    unbound = await store.bind_account_user(first.account_id, None, actor_account_id=first.account_id)
    assert unbound.bound_user_id is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


async def test_missing_plugin_nodes_remain_configured_but_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "metadata.sqlite3"
    store = WebUiStore(path, build_default_registry())
    account = await store.create_registered_account(
        login_name="pluginuser",
        display_name="Plugin User",
        password_hash=PasswordService().hash("PluginUser!123"),
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO webui_permission_nodes(permission_key, description, plugin_id, available, "
            "custom_assignable, modified_at) VALUES ('webui.external.use', 'External', 'webui.external', 1, 1, 'now')"
        )
        db.execute(
            "INSERT INTO webui_permission_groups(group_key, description, system_managed, modified_at) "
            "VALUES ('webui.custom.external', 'External group', 0, 'now')"
        )
        db.execute(
            "INSERT INTO webui_group_permissions VALUES ('webui.custom.external', 'webui.external.use')"
        )
        db.execute(
            "INSERT INTO webui_account_groups VALUES (?, 'webui.custom.external')", (account.account_id,)
        )
    reloaded = WebUiStore(path, build_default_registry())
    await reloaded.ensure()
    assert "webui.external.use" not in await reloaded.permission_keys(account.account_id)
    catalog = await reloaded.permission_catalog()
    node = next(item for item in catalog.nodes if item.key == "webui.external.use")
    group = next(item for item in catalog.groups if item.key == "webui.custom.external")
    assert node.available is False
    assert group.permissions == ["webui.external.use"]


def test_plugin_manifest_dependency_is_enforced() -> None:
    class MissingDependencyPlugin:
        manifest = PluginManifest("webui.external", "1.0.0", ("webui.missing",))

        def register(self, registry: PermissionRegistry) -> None:
            registry.register_node("webui.external.use", "External")

    registry = PermissionRegistry()
    try:
        registry.register_plugin(MissingDependencyPlugin())
    except ValueError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover - 断言保护
        raise AssertionError("missing plugin dependency was accepted")
