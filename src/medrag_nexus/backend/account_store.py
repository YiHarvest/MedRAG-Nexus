"""保存后端账号、Session、权限和审计的 SQLite 存储。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .account_models import (
    AccountRecord,
    AdminPatchAccountRequest,
    AuditEventResponse,
    PatchPermissionGroupRequest,
    PermissionCatalogResponse,
    PermissionGroupResponse,
    PermissionLevelResponse,
    PermissionNodeResponse,
    PermissionPluginResponse,
)
from .audit import current_audit_request_id
from .permissions import PermissionRegistry
from .security import new_session_token, session_token_hash

_LEGACY_ROLE_GROUPS = (
    "webui.registered",
    "webui.editor",
    "webui.workspace_manager",
    "webui.superadmin",
)
_USER_RESOURCE_ACTIONS = (
    "webui.user.read",
    "webui.workspace.create",
    "webui.user.rename",
    "webui.user.delete",
    "webui.user.policy.manage",
)
_WORKSPACE_RESOURCE_ACTIONS = (
    "webui.workspace.read",
    "webui.workspace.rename",
    "webui.workspace.delete",
    "webui.workspace.policy.manage",
    "webui.resource.file.add",
    "webui.resource.file.download",
    "webui.resource.file.delete",
    "webui.resource.text.add",
    "webui.resource.text.delete",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class AccountStoreError(RuntimeError):
    code = "webui_store_error"


class AccountConflictError(AccountStoreError):
    code = "account_conflict"


class AccountNotFoundError(AccountStoreError):
    code = "account_not_found"


class InvalidPermissionGroupError(AccountStoreError):
    code = "invalid_permission_group"


class LastSuperadminError(AccountStoreError):
    code = "last_superadmin_required"


class SuperadminImmutableError(AccountStoreError):
    code = "superadmin_immutable"


class InvalidPermissionLevelError(AccountStoreError):
    code = "invalid_permission_level"


class PermissionGroupConflictError(AccountStoreError):
    code = "permission_group_conflict"


class PermissionGroupNotFoundError(AccountStoreError):
    code = "permission_group_not_found"


class AccountBindingError(AccountStoreError):
    code = "account_binding_error"


class AccountStore:
    """在现有元数据 SQLite 文件中管理后端账号与权限表。

    不修改既有业务表。``ensure`` 可以重复调用，并会把插件注册器中的权限定义
    同步到旁路权限表。
    """

    def __init__(self, path: Path, registry: PermissionRegistry):
        self.path = path
        self.registry = registry
        self._ensured = False
        self._ensure_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    async def ensure(self) -> None:
        if self._ensured:
            return
        async with self._ensure_lock:
            if self._ensured:
                return
            await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(self._ensure_sync)
            self._ensured = True

    def _ensure_sync(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            had_legacy_binding_table = bool(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'webui_account_user_responsibilities'"
                ).fetchone()
            )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS webui_accounts (
                    account_id TEXT PRIMARY KEY,
                    login_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    permission_level INTEGER NOT NULL DEFAULT 0 CHECK(permission_level >= 0),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    bound_user_id TEXT,
                    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0, 1)),
                    credential_version INTEGER NOT NULL DEFAULT 1 CHECK(credential_version > 0),
                    failed_login_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_login_count >= 0),
                    locked_until TEXT,
                    password_changed_at TEXT NOT NULL,
                    last_login_at TEXT,
                    created_at TEXT NOT NULL,
                    modified_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS webui_sessions (
                    session_id_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES webui_accounts(account_id) ON DELETE CASCADE,
                    credential_version INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_webui_sessions_account ON webui_sessions(account_id);
                CREATE INDEX IF NOT EXISTS idx_webui_sessions_expires ON webui_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS webui_permission_nodes (
                    permission_key TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0, 1)),
                    custom_assignable INTEGER NOT NULL DEFAULT 1 CHECK(custom_assignable IN (0, 1)),
                    modified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webui_permission_plugins (
                    plugin_id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    requires_json TEXT NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0, 1)),
                    modified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webui_permission_levels (
                    level INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0, 1)),
                    modified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webui_permission_groups (
                    group_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL,
                    system_managed INTEGER NOT NULL DEFAULT 0 CHECK(system_managed IN (0, 1)),
                    modified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webui_group_permissions (
                    group_key TEXT NOT NULL REFERENCES webui_permission_groups(group_key) ON DELETE CASCADE,
                    permission_key TEXT NOT NULL REFERENCES webui_permission_nodes(permission_key) ON DELETE CASCADE,
                    PRIMARY KEY(group_key, permission_key)
                );
                CREATE TABLE IF NOT EXISTS webui_account_groups (
                    account_id TEXT NOT NULL REFERENCES webui_accounts(account_id) ON DELETE CASCADE,
                    group_key TEXT NOT NULL REFERENCES webui_permission_groups(group_key),
                    PRIMARY KEY(account_id, group_key)
                );
                CREATE TABLE IF NOT EXISTS webui_account_user_bindings (
                    account_id TEXT NOT NULL REFERENCES webui_accounts(account_id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    assigned_by_account_id TEXT REFERENCES webui_accounts(account_id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_webui_account_user_bindings_user
                    ON webui_account_user_bindings(user_id);

                CREATE TABLE IF NOT EXISTS webui_user_policies (
                    user_id TEXT PRIMARY KEY,
                    read_min_level INTEGER NOT NULL DEFAULT 1000 CHECK(read_min_level >= 0),
                    workspace_create_min_level INTEGER NOT NULL DEFAULT 1000 CHECK(workspace_create_min_level >= 0),
                    policy_version INTEGER NOT NULL DEFAULT 1,
                    modified_by_account_id TEXT REFERENCES webui_accounts(account_id),
                    modified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webui_workspace_policies (
                    workspace_id TEXT PRIMARY KEY,
                    read_min_level INTEGER NOT NULL DEFAULT 1000 CHECK(read_min_level >= 0),
                    cud_min_level INTEGER NOT NULL DEFAULT 1000 CHECK(cud_min_level >= 0),
                    policy_version INTEGER NOT NULL DEFAULT 1,
                    created_by_account_id TEXT REFERENCES webui_accounts(account_id),
                    modified_by_account_id TEXT REFERENCES webui_accounts(account_id),
                    created_at TEXT NOT NULL,
                    modified_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS webui_policy_bindings (
                    binding_id TEXT PRIMARY KEY,
                    resource_type TEXT NOT NULL CHECK(resource_type IN ('user', 'workspace')),
                    resource_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    principal_type TEXT NOT NULL CHECK(principal_type IN ('account', 'group')),
                    principal_id TEXT NOT NULL,
                    effect TEXT NOT NULL CHECK(effect IN ('allow', 'deny')),
                    managed_by TEXT,
                    immutable INTEGER NOT NULL DEFAULT 0 CHECK(immutable IN (0, 1)),
                    created_by_account_id TEXT REFERENCES webui_accounts(account_id),
                    created_at TEXT NOT NULL,
                    UNIQUE(resource_type, resource_id, action, principal_type, principal_id)
                );
                CREATE INDEX IF NOT EXISTS idx_webui_policy_resource
                    ON webui_policy_bindings(resource_type, resource_id, action);

                CREATE TABLE IF NOT EXISTS webui_audit_events (
                    event_id TEXT PRIMARY KEY,
                    actor_account_id TEXT REFERENCES webui_accounts(account_id),
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    request_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_webui_audit_created ON webui_audit_events(created_at);
                """
            )
            if had_legacy_binding_table:
                db.execute(
                    "INSERT OR IGNORE INTO webui_account_user_bindings"
                    "(account_id, user_id, assigned_by_account_id, created_at) "
                    "SELECT account_id, user_id, assigned_by_account_id, created_at "
                    "FROM webui_account_user_responsibilities"
                )
                db.execute("DROP TABLE webui_account_user_responsibilities")
            db.execute(
                "INSERT OR IGNORE INTO webui_account_user_bindings"
                "(account_id, user_id, assigned_by_account_id, created_at) "
                "SELECT account_id, bound_user_id, account_id, modified_at FROM webui_accounts "
                "WHERE bound_user_id IS NOT NULL"
            )
            self._ensure_column(
                db, "webui_permission_nodes", "available", "INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0, 1))"
            )
            self._ensure_column(
                db,
                "webui_permission_nodes",
                "custom_assignable",
                "INTEGER NOT NULL DEFAULT 1 CHECK(custom_assignable IN (0, 1))",
            )
            self._ensure_column(
                db,
                "webui_permission_groups",
                "system_managed",
                "INTEGER NOT NULL DEFAULT 0 CHECK(system_managed IN (0, 1))",
            )
            self._ensure_column(db, "webui_permission_groups", "name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "webui_policy_bindings", "managed_by", "TEXT")
            self._ensure_column(
                db,
                "webui_policy_bindings",
                "immutable",
                "INTEGER NOT NULL DEFAULT 0 CHECK(immutable IN (0, 1))",
            )
            db.execute("UPDATE webui_permission_groups SET name = description WHERE name = ''")
            self._migrate_policy_bindings_v2(db)
            now = _iso(_now())
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DROP INDEX IF EXISTS idx_webui_accounts_bound_user")
                db.execute(
                    "UPDATE webui_accounts SET bound_user_id = NULL, modified_at = ? "
                    "WHERE permission_level >= 1000 AND bound_user_id IS NOT NULL",
                    (now,),
                )
                db.execute(
                    "DELETE FROM webui_account_user_bindings "
                    "WHERE account_id IN (SELECT account_id FROM webui_accounts WHERE permission_level >= 1000)"
                )
                db.execute(
                    "UPDATE webui_accounts SET bound_user_id = ("
                    "SELECT MIN(binding.user_id) FROM webui_account_user_bindings AS binding "
                    "WHERE binding.account_id = webui_accounts.account_id"
                    ") WHERE permission_level < 1000"
                )
                db.execute("UPDATE webui_permission_plugins SET available = 0, modified_at = ?", (now,))
                db.execute("UPDATE webui_permission_nodes SET available = 0, modified_at = ?", (now,))
                db.execute("UPDATE webui_permission_levels SET available = 0, modified_at = ?", (now,))
                for plugin in self.registry.plugins:
                    db.execute(
                        "INSERT INTO webui_permission_plugins"
                        "(plugin_id, version, requires_json, available, modified_at) "
                        "VALUES (?, ?, ?, 1, ?) ON CONFLICT(plugin_id) DO UPDATE SET version=excluded.version, "
                        "requires_json=excluded.requires_json, available=1, modified_at=excluded.modified_at",
                        (plugin.plugin_id, plugin.version, json.dumps(plugin.requires), now),
                    )
                for level in self.registry.levels:
                    db.execute(
                        "INSERT INTO webui_permission_levels(level, name, description, available, modified_at) "
                        "VALUES (?, ?, ?, 1, ?) ON CONFLICT(level) DO UPDATE SET name=excluded.name, "
                        "description=excluded.description, available=1, modified_at=excluded.modified_at",
                        (level.value, level.name, level.description, now),
                    )
                for node in self.registry.nodes:
                    db.execute(
                        "INSERT INTO webui_permission_nodes(permission_key, description, plugin_id, available, "
                        "custom_assignable, modified_at) VALUES (?, ?, ?, 1, ?, ?) "
                        "ON CONFLICT(permission_key) DO UPDATE SET "
                        "description=excluded.description, plugin_id=excluded.plugin_id, "
                        "available=1, custom_assignable=excluded.custom_assignable, "
                        "modified_at=excluded.modified_at",
                        (node.key, node.description, node.plugin_id, int(node.custom_assignable), now),
                    )
                for group in self.registry.groups:
                    db.execute(
                        "INSERT INTO webui_permission_groups"
                        "(group_key, name, description, system_managed, modified_at) "
                        "VALUES (?, ?, ?, 1, ?) ON CONFLICT(group_key) DO UPDATE SET "
                        "name=excluded.name, description=excluded.description, system_managed=1, "
                        "modified_at=excluded.modified_at",
                        (group.key, group.description, group.description, now),
                    )
                    db.execute("DELETE FROM webui_group_permissions WHERE group_key = ?", (group.key,))
                    db.executemany(
                        "INSERT INTO webui_group_permissions(group_key, permission_key) VALUES (?, ?)",
                        [(group.key, permission) for permission in sorted(group.permissions)],
                    )
                self._migrate_retired_permissions_and_levels(db, now)
                self._migrate_legacy_role_groups(db, now)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _migrate_retired_permissions_and_levels(self, db: sqlite3.Connection, now: str) -> None:
        """清理明确退役的核心权限，并把已取消的等级 3 收敛到等级 2。"""

        deprecated_permission = "webui.account.sessions.revoke"
        affected = {
            "accounts": int(db.execute("SELECT COUNT(*) FROM webui_accounts WHERE permission_level = 3").fetchone()[0]),
            "user_policies": int(
                db.execute(
                    "SELECT COUNT(*) FROM webui_user_policies "
                    "WHERE read_min_level = 3 OR workspace_create_min_level = 3"
                ).fetchone()[0]
            ),
            "workspace_policies": int(
                db.execute(
                    "SELECT COUNT(*) FROM webui_workspace_policies WHERE read_min_level = 3 OR cud_min_level = 3"
                ).fetchone()[0]
            ),
            "permission_groups": int(
                db.execute(
                    "SELECT COUNT(*) FROM webui_group_permissions WHERE permission_key = ?",
                    (deprecated_permission,),
                ).fetchone()[0]
            ),
        }
        db.execute(
            "UPDATE webui_accounts SET permission_level = 2, modified_at = ? WHERE permission_level = 3",
            (now,),
        )
        db.execute(
            "UPDATE webui_user_policies SET "
            "read_min_level = CASE WHEN read_min_level = 3 THEN 2 ELSE read_min_level END, "
            "workspace_create_min_level = CASE WHEN workspace_create_min_level = 3 THEN 2 "
            "ELSE workspace_create_min_level END, modified_at = ? "
            "WHERE read_min_level = 3 OR workspace_create_min_level = 3",
            (now,),
        )
        db.execute(
            "UPDATE webui_workspace_policies SET "
            "read_min_level = CASE WHEN read_min_level = 3 THEN 2 ELSE read_min_level END, "
            "cud_min_level = CASE WHEN cud_min_level = 3 THEN 2 ELSE cud_min_level END, modified_at = ? "
            "WHERE read_min_level = 3 OR cud_min_level = 3",
            (now,),
        )
        db.execute("DELETE FROM webui_permission_levels WHERE level = 3")
        db.execute(
            "DELETE FROM webui_group_permissions WHERE permission_key = ?",
            (deprecated_permission,),
        )
        removed_node = db.execute(
            "DELETE FROM webui_permission_nodes WHERE permission_key = ?",
            (deprecated_permission,),
        ).rowcount
        if any(affected.values()) or removed_node:
            self._audit(
                db,
                None,
                "webui.permission_model.migrate",
                "permission_model",
                "levels-v3",
                {"levels": [0, 1, 2, 3, 1000], "deprecated_permission": deprecated_permission},
                {"levels": [0, 1, 2, 1000], "affected": affected},
            )

    def _migrate_legacy_role_groups(self, db: sqlite3.Connection, now: str) -> None:
        """把旧内置角色组迁移为成员等级权限，并保留超级管理员的显式 ACL。"""

        placeholders = ",".join("?" for _ in _LEGACY_ROLE_GROUPS)
        memberships = int(
            db.execute(
                f"SELECT COUNT(*) FROM webui_account_groups WHERE group_key IN ({placeholders})",
                _LEGACY_ROLE_GROUPS,
            ).fetchone()[0]
        )
        groups = int(
            db.execute(
                f"SELECT COUNT(*) FROM webui_permission_groups WHERE group_key IN ({placeholders})",
                _LEGACY_ROLE_GROUPS,
            ).fetchone()[0]
        )
        legacy_superadmin_bindings = db.execute(
            "SELECT resource_type, resource_id, action FROM webui_policy_bindings "
            "WHERE principal_type = 'group' AND principal_id = 'webui.superadmin' AND effect = 'allow'"
        ).fetchall()
        superadmins = [
            str(row[0])
            for row in db.execute(
                "SELECT account_id FROM webui_accounts WHERE enabled = 1 AND permission_level = 1000"
            ).fetchall()
        ]
        for account_id in superadmins:
            db.executemany(
                "INSERT OR IGNORE INTO webui_policy_bindings"
                "(binding_id, resource_type, resource_id, action, principal_type, principal_id, effect, "
                "managed_by, immutable, created_by_account_id, created_at) "
                "VALUES (lower(hex(randomblob(16))), ?, ?, ?, 'account', ?, 'allow', "
                "'system.superadmin', 1, NULL, ?)",
                [
                    (str(row["resource_type"]), str(row["resource_id"]), str(row["action"]), account_id, now)
                    for row in legacy_superadmin_bindings
                ],
            )
        db.execute(
            f"DELETE FROM webui_policy_bindings WHERE principal_type = 'group' AND principal_id IN ({placeholders})",
            _LEGACY_ROLE_GROUPS,
        )
        db.execute(
            f"DELETE FROM webui_account_groups WHERE group_key IN ({placeholders})",
            _LEGACY_ROLE_GROUPS,
        )
        db.execute(
            f"DELETE FROM webui_group_permissions WHERE group_key IN ({placeholders})",
            _LEGACY_ROLE_GROUPS,
        )
        db.execute(
            f"DELETE FROM webui_permission_groups WHERE group_key IN ({placeholders})",
            _LEGACY_ROLE_GROUPS,
        )
        if memberships or groups or legacy_superadmin_bindings:
            self._audit(
                db,
                None,
                "webui.permission_roles.migrate",
                "permission_model",
                "level-permissions",
                {"legacy_role_groups": list(_LEGACY_ROLE_GROUPS)},
                {
                    "permission_groups": 0,
                    "migrated_memberships": memberships,
                    "migrated_superadmin_acls": len(legacy_superadmin_bindings) * len(superadmins),
                },
            )

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _migrate_policy_bindings_v2(db: sqlite3.Connection) -> None:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='webui_policy_bindings'").fetchone()
        schema = str(row[0]) if row and row[0] else ""
        if "managed_by" in schema and "'level'" not in schema and "CHECK(action IN" not in schema:
            return
        db.execute("ALTER TABLE webui_policy_bindings RENAME TO webui_policy_bindings_v1")
        db.executescript(
            """
            CREATE TABLE webui_policy_bindings (
                binding_id TEXT PRIMARY KEY,
                resource_type TEXT NOT NULL CHECK(resource_type IN ('user', 'workspace')),
                resource_id TEXT NOT NULL,
                action TEXT NOT NULL,
                principal_type TEXT NOT NULL CHECK(principal_type IN ('account', 'group')),
                principal_id TEXT NOT NULL,
                effect TEXT NOT NULL CHECK(effect IN ('allow', 'deny')),
                managed_by TEXT,
                immutable INTEGER NOT NULL DEFAULT 0 CHECK(immutable IN (0, 1)),
                created_by_account_id TEXT REFERENCES webui_accounts(account_id),
                created_at TEXT NOT NULL,
                UNIQUE(resource_type, resource_id, action, principal_type, principal_id)
            );
            INSERT INTO webui_policy_bindings(
                binding_id, resource_type, resource_id, action, principal_type, principal_id,
                effect, managed_by, immutable, created_by_account_id, created_at
            )
            SELECT binding_id, resource_type, resource_id, action, principal_type, principal_id,
                   effect, managed_by, immutable, created_by_account_id, created_at
            FROM webui_policy_bindings_v1
            WHERE principal_type IN ('account', 'group');
            DROP TABLE webui_policy_bindings_v1;
            DROP INDEX IF EXISTS idx_webui_policy_resource;
            CREATE INDEX idx_webui_policy_resource
                ON webui_policy_bindings(resource_type, resource_id, action);
            """
        )

    async def register_account(self, *, login_name: str, display_name: str, password_hash: str) -> AccountRecord:
        return await self.create_account(
            login_name=login_name,
            display_name=display_name,
            password_hash=password_hash,
            permission_level=0,
            group_keys=[],
            bound_user_id=None,
            must_change_password=False,
            actor_account_id=None,
            audit_action="webui.account.register",
        )

    async def bootstrap_superadmin(self, *, login_name: str, display_name: str, password_hash: str) -> AccountRecord:
        """仅在账号表为空时创建首个超级管理员。"""

        await self.ensure()

        def create() -> AccountRecord:
            with self._connect() as db:
                if db.execute("SELECT 1 FROM webui_accounts LIMIT 1").fetchone():
                    raise AccountConflictError("bootstrap is only allowed before the first account exists")
            return self._create_account_sync(
                login_name=login_name,
                display_name=display_name,
                password_hash=password_hash,
                permission_level=1000,
                group_keys=[],
                bound_user_id=None,
                must_change_password=False,
                actor_account_id=None,
                audit_action="webui.account.bootstrap_superadmin",
            )

        return await asyncio.to_thread(create)

    async def create_account(
        self,
        *,
        login_name: str,
        display_name: str,
        password_hash: str,
        permission_level: int,
        group_keys: list[str],
        bound_user_id: str | None,
        must_change_password: bool,
        actor_account_id: str | None,
        audit_action: str = "webui.account.create",
    ) -> AccountRecord:
        await self.ensure()
        return await asyncio.to_thread(
            self._create_account_sync,
            login_name=login_name,
            display_name=display_name,
            password_hash=password_hash,
            permission_level=permission_level,
            group_keys=group_keys,
            bound_user_id=bound_user_id,
            must_change_password=must_change_password,
            actor_account_id=actor_account_id,
            audit_action=audit_action,
        )

    def _create_account_sync(
        self,
        *,
        login_name: str,
        display_name: str,
        password_hash: str,
        permission_level: int,
        group_keys: list[str],
        bound_user_id: str | None,
        must_change_password: bool,
        actor_account_id: str | None,
        audit_action: str,
    ) -> AccountRecord:
        account_id = uuid4().hex
        now = _iso(_now())
        groups = sorted(set(group_keys))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._validate_groups(db, groups)
                self._validate_level(permission_level)
                self._validate_superadmin_shape(permission_level, groups)
                resolved_user_id = bound_user_id
                if permission_level >= 1000 and resolved_user_id is not None:
                    raise AccountBindingError("superadmin accounts do not use knowledge domain bindings")
                if resolved_user_id is not None:
                    self._validate_binding_target(db, resolved_user_id)
                db.execute(
                    "INSERT INTO webui_accounts(account_id, login_name, display_name, password_hash, permission_level, "
                    "enabled, bound_user_id, must_change_password, credential_version, failed_login_count, "
                    "password_changed_at, created_at, modified_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 1, 0, ?, ?, ?)",
                    (
                        account_id,
                        login_name,
                        display_name,
                        password_hash,
                        permission_level,
                        resolved_user_id,
                        int(must_change_password),
                        now,
                        now,
                        now,
                    ),
                )
                db.executemany(
                    "INSERT INTO webui_account_groups(account_id, group_key) VALUES (?, ?)",
                    [(account_id, group) for group in groups],
                )
                if permission_level == 1000:
                    self._grant_superadmin_existing_resource_acls(db, account_id, now)
                self._audit(
                    db,
                    actor_account_id,
                    audit_action,
                    "account",
                    account_id,
                    None,
                    {"login_name": login_name, "permission_level": permission_level, "groups": groups},
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                if "login_name" in str(exc) or "UNIQUE" in str(exc):
                    raise AccountConflictError("login_name already exists") from exc
                raise
            except BaseException:
                db.rollback()
                raise
        account = self._get_account_sync(account_id)
        if account is None:  # pragma: no cover - 事务不变量保护
            raise RuntimeError("created account could not be read")
        return account

    @staticmethod
    def _grant_superadmin_existing_resource_acls(
        db: sqlite3.Connection,
        account_id: str,
        now: str,
    ) -> None:
        """新建超级管理员时，为全部既有知识资源写入不可变的账号 ACL。"""

        table_names = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('users', 'workspaces')"
            ).fetchall()
        }
        resources: list[tuple[str, str, tuple[str, ...]]] = []
        if "users" in table_names:
            resources.extend(
                ("user", str(row[0]), _USER_RESOURCE_ACTIONS)
                for row in db.execute("SELECT user_id FROM users").fetchall()
            )
        if "workspaces" in table_names:
            resources.extend(
                ("workspace", str(row[0]), _WORKSPACE_RESOURCE_ACTIONS)
                for row in db.execute("SELECT workspace_id FROM workspaces").fetchall()
            )
        db.executemany(
            "INSERT OR IGNORE INTO webui_policy_bindings"
            "(binding_id, resource_type, resource_id, action, principal_type, principal_id, effect, "
            "managed_by, immutable, created_by_account_id, created_at) "
            "VALUES (lower(hex(randomblob(16))), ?, ?, ?, 'account', ?, 'allow', "
            "'system.superadmin', 1, NULL, ?)",
            [
                (resource_type, resource_id, action, account_id, now)
                for resource_type, resource_id, actions in resources
                for action in actions
            ],
        )

    def _validate_level(self, level: int) -> None:
        if self.registry.level(level) is None:
            allowed = ", ".join(str(item.value) for item in self.registry.levels)
            raise InvalidPermissionLevelError(f"permission_level must be one of: {allowed}")

    @staticmethod
    def _validate_superadmin_shape(level: int, groups: list[str]) -> None:
        if level == 1000 and groups:
            raise InvalidPermissionGroupError("superadmin accounts cannot join organizational permission groups")

    @staticmethod
    def _validate_binding_target(db: sqlite3.Connection, user_id: str) -> None:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
            raise AccountBindingError("knowledge users table does not exist")
        if not db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone():
            raise AccountBindingError("knowledge user does not exist")

    async def get_account(self, account_id: str) -> AccountRecord | None:
        await self.ensure()
        return await asyncio.to_thread(self._get_account_sync, account_id)

    async def get_account_by_login(self, login_name: str) -> AccountRecord | None:
        await self.ensure()

        def read() -> AccountRecord | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT * FROM webui_accounts WHERE login_name = ? COLLATE NOCASE", (login_name,)
                ).fetchone()
                return self._account(db, row) if row else None

        return await asyncio.to_thread(read)

    async def list_accounts(self) -> list[AccountRecord]:
        await self.ensure()

        def read() -> list[AccountRecord]:
            with self._connect() as db:
                rows = db.execute("SELECT * FROM webui_accounts ORDER BY created_at ASC, login_name ASC").fetchall()
                return [self._account(db, row) for row in rows]

        return await asyncio.to_thread(read)

    async def permission_keys(self, account_id: str) -> set[str]:
        await self.ensure()

        def read() -> set[str]:
            with self._connect() as db:
                account = db.execute(
                    "SELECT permission_level FROM webui_accounts WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                if account is None:
                    return set()
                level = self.registry.level(int(account["permission_level"]))
                permissions = set(level.permissions if level is not None else ())
                rows = db.execute(
                    "SELECT DISTINCT gp.permission_key FROM webui_account_groups ag "
                    "JOIN webui_group_permissions gp ON gp.group_key = ag.group_key "
                    "JOIN webui_permission_nodes pn ON pn.permission_key = gp.permission_key "
                    "WHERE ag.account_id = ? AND pn.available = 1",
                    (account_id,),
                ).fetchall()
                permissions.update(str(row[0]) for row in rows)
                return permissions

        return await asyncio.to_thread(read)

    async def permission_catalog(self) -> PermissionCatalogResponse:
        await self.ensure()

        def read() -> PermissionCatalogResponse:
            with self._connect() as db:
                plugins = [
                    PermissionPluginResponse(
                        plugin_id=str(row["plugin_id"]),
                        version=str(row["version"]),
                        requires=list(json.loads(row["requires_json"])),
                    )
                    for row in db.execute(
                        "SELECT plugin_id, version, requires_json FROM webui_permission_plugins "
                        "WHERE available = 1 ORDER BY plugin_id"
                    ).fetchall()
                ]
                levels = [
                    PermissionLevelResponse(
                        value=level.value,
                        name=level.name,
                        description=level.description,
                        permissions=sorted(level.permissions),
                    )
                    for level in self.registry.levels
                ]
                nodes = [
                    PermissionNodeResponse(
                        key=str(row["permission_key"]),
                        description=str(row["description"]),
                        plugin_id=str(row["plugin_id"]),
                        available=bool(row["available"]),
                        custom_assignable=bool(row["custom_assignable"]),
                    )
                    for row in db.execute(
                        "SELECT permission_key, description, plugin_id, available, custom_assignable "
                        "FROM webui_permission_nodes ORDER BY plugin_id, permission_key"
                    ).fetchall()
                ]
                groups = self._permission_groups(db)
            return PermissionCatalogResponse(plugins=plugins, levels=levels, nodes=nodes, groups=groups)

        return await asyncio.to_thread(read)

    async def list_permission_groups(self) -> list[PermissionGroupResponse]:
        await self.ensure()

        def read() -> list[PermissionGroupResponse]:
            with self._connect() as db:
                return self._permission_groups(db)

        return await asyncio.to_thread(read)

    async def create_permission_group(
        self,
        *,
        group_key: str,
        name: str | None = None,
        description: str,
        permissions: list[str],
        actor_account_id: str,
    ) -> PermissionGroupResponse:
        await self.ensure()

        def write() -> PermissionGroupResponse:
            now = _iso(_now())
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    if db.execute("SELECT 1 FROM webui_permission_groups WHERE group_key = ?", (group_key,)).fetchone():
                        raise PermissionGroupConflictError("permission group already exists")
                    normalized = sorted(set(permissions))
                    self._validate_custom_permissions(db, normalized)
                    display_name = (name or description or group_key).strip()
                    db.execute(
                        "INSERT INTO webui_permission_groups"
                        "(group_key, name, description, system_managed, modified_at) "
                        "VALUES (?, ?, ?, 0, ?)",
                        (group_key, display_name, description, now),
                    )
                    db.executemany(
                        "INSERT INTO webui_group_permissions(group_key, permission_key) VALUES (?, ?)",
                        [(group_key, permission) for permission in normalized],
                    )
                    self._audit(
                        db,
                        actor_account_id,
                        "webui.permission_group.create",
                        "permission_group",
                        group_key,
                        None,
                        {"name": display_name, "description": description, "permissions": normalized},
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            return self._get_permission_group_sync(group_key)

        return await asyncio.to_thread(write)

    async def patch_permission_group(
        self,
        *,
        group_key: str,
        patch: PatchPermissionGroupRequest,
        actor_account_id: str,
    ) -> PermissionGroupResponse:
        await self.ensure()

        def write() -> PermissionGroupResponse:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute(
                        "SELECT name, description, system_managed FROM webui_permission_groups WHERE group_key = ?",
                        (group_key,),
                    ).fetchone()
                    if row is None:
                        raise PermissionGroupNotFoundError("permission group does not exist")
                    if bool(row["system_managed"]):
                        raise PermissionGroupConflictError("system-managed permission groups cannot be modified")
                    old_permissions = self._group_permission_keys(db, group_key)
                    name = str(row["name"]) if patch.name is None else patch.name
                    description = str(row["description"]) if patch.description is None else patch.description
                    permissions = old_permissions if patch.permissions is None else sorted(set(patch.permissions))
                    self._validate_custom_permissions(db, permissions)
                    db.execute(
                        "UPDATE webui_permission_groups SET name = ?, description = ?, modified_at = ? "
                        "WHERE group_key = ?",
                        (name, description, _iso(_now()), group_key),
                    )
                    if patch.permissions is not None:
                        db.execute("DELETE FROM webui_group_permissions WHERE group_key = ?", (group_key,))
                        db.executemany(
                            "INSERT INTO webui_group_permissions(group_key, permission_key) VALUES (?, ?)",
                            [(group_key, permission) for permission in permissions],
                        )
                    self._audit(
                        db,
                        actor_account_id,
                        "webui.permission_group.update",
                        "permission_group",
                        group_key,
                        {
                            "name": str(row["name"]),
                            "description": str(row["description"]),
                            "permissions": old_permissions,
                        },
                        {"name": name, "description": description, "permissions": permissions},
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            return self._get_permission_group_sync(group_key)

        return await asyncio.to_thread(write)

    async def delete_permission_group(self, *, group_key: str, actor_account_id: str) -> None:
        await self.ensure()

        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute(
                        "SELECT description, system_managed FROM webui_permission_groups WHERE group_key = ?",
                        (group_key,),
                    ).fetchone()
                    if row is None:
                        raise PermissionGroupNotFoundError("permission group does not exist")
                    if bool(row["system_managed"]):
                        raise PermissionGroupConflictError("system-managed permission groups cannot be deleted")
                    if db.execute(
                        "SELECT 1 FROM webui_account_groups WHERE group_key = ? LIMIT 1", (group_key,)
                    ).fetchone():
                        raise PermissionGroupConflictError("permission group is still assigned to accounts")
                    before = {
                        "description": str(row["description"]),
                        "permissions": self._group_permission_keys(db, group_key),
                    }
                    db.execute("DELETE FROM webui_permission_groups WHERE group_key = ?", (group_key,))
                    self._audit(
                        db,
                        actor_account_id,
                        "webui.permission_group.delete",
                        "permission_group",
                        group_key,
                        before,
                        None,
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)

    async def leave_permission_group(self, *, account_id: str, group_key: str) -> AccountRecord:
        """让普通账号退出自定义权限组，并立即撤销该组继承的权限。"""

        await self.ensure()

        def write() -> AccountRecord:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    account_row = db.execute(
                        "SELECT * FROM webui_accounts WHERE account_id = ?", (account_id,)
                    ).fetchone()
                    if account_row is None:
                        raise AccountNotFoundError("account does not exist")
                    account = self._account(db, account_row)
                    if self._is_superadmin(account):
                        raise SuperadminImmutableError("superadmin permissions cannot be changed")
                    group_row = db.execute(
                        "SELECT name, system_managed FROM webui_permission_groups WHERE group_key = ?",
                        (group_key,),
                    ).fetchone()
                    if group_row is None:
                        raise PermissionGroupNotFoundError("permission group does not exist")
                    if bool(group_row["system_managed"]):
                        raise PermissionGroupConflictError("system-managed permission groups cannot be left")
                    removed = db.execute(
                        "DELETE FROM webui_account_groups WHERE account_id = ? AND group_key = ?",
                        (account_id, group_key),
                    ).rowcount
                    if removed:
                        self._audit(
                            db,
                            account_id,
                            "webui.permission_group.leave_self",
                            "permission_group",
                            group_key,
                            {"member_account_id": account_id, "member": True},
                            {"member_account_id": account_id, "member": False},
                        )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            updated = self._get_account_sync(account_id)
            if updated is None:  # pragma: no cover - 同一事务中账号不会消失
                raise AccountNotFoundError("account does not exist")
            return updated

        return await asyncio.to_thread(write)

    async def set_account_user_bindings(
        self,
        account_id: str,
        user_ids: list[str],
        actor_account_id: str,
    ) -> AccountRecord:
        """原子替换普通账号的全部知识域绑定。"""

        await self.ensure()
        normalized_user_ids = sorted(set(user_ids))

        def write() -> AccountRecord:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute(
                        "SELECT permission_level FROM webui_accounts WHERE account_id = ?",
                        (account_id,),
                    ).fetchone()
                    if row is None:
                        raise AccountNotFoundError("account does not exist")
                    if int(row["permission_level"]) >= 1000:
                        raise AccountBindingError("superadmin accounts do not use knowledge domain bindings")
                    before_user_ids = [
                        str(item[0])
                        for item in db.execute(
                            "SELECT user_id FROM webui_account_user_bindings WHERE account_id = ? ORDER BY user_id",
                            (account_id,),
                        ).fetchall()
                    ]
                    for user_id in normalized_user_ids:
                        self._validate_binding_target(db, user_id)
                    now = _iso(_now())
                    db.execute("DELETE FROM webui_account_user_bindings WHERE account_id = ?", (account_id,))
                    db.executemany(
                        "INSERT INTO webui_account_user_bindings"
                        "(account_id, user_id, assigned_by_account_id, created_at) VALUES (?, ?, ?, ?)",
                        [(account_id, user_id, actor_account_id, now) for user_id in normalized_user_ids],
                    )
                    db.execute(
                        "UPDATE webui_accounts SET bound_user_id = ?, modified_at = ? WHERE account_id = ?",
                        (normalized_user_ids[0] if normalized_user_ids else None, now, account_id),
                    )
                    self._audit(
                        db,
                        actor_account_id,
                        "webui.account.user_bindings.update",
                        "account",
                        account_id,
                        {"bound_user_ids": before_user_ids},
                        {"bound_user_ids": normalized_user_ids},
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            account = self._get_account_sync(account_id)
            if account is None:
                raise AccountNotFoundError("account does not exist")
            return account

        return await asyncio.to_thread(write)

    async def bind_account_user(self, account_id: str, user_id: str | None, actor_account_id: str) -> AccountRecord:
        """兼容单绑定调用方；新代码应使用多绑定接口。"""

        return await self.set_account_user_bindings(
            account_id,
            [user_id] if user_id is not None else [],
            actor_account_id,
        )

    async def remove_user_bindings(self, user_id: str) -> None:
        """知识域删除后清理全部普通账号绑定。"""

        await self.ensure()
        await asyncio.to_thread(lambda: self._delete_user_bindings_sync(user_id))

    def _delete_user_bindings_sync(self, user_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM webui_account_user_bindings WHERE user_id = ?", (user_id,))
            db.execute(
                "UPDATE webui_accounts SET bound_user_id = ("
                "SELECT MIN(binding.user_id) FROM webui_account_user_bindings AS binding "
                "WHERE binding.account_id = webui_accounts.account_id"
                ") WHERE bound_user_id = ?",
                (user_id,),
            )
            db.commit()

    async def patch_account(
        self, *, account_id: str, patch: AdminPatchAccountRequest, actor_account_id: str
    ) -> AccountRecord:
        await self.ensure()

        def write() -> AccountRecord:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute("SELECT * FROM webui_accounts WHERE account_id = ?", (account_id,)).fetchone()
                    if row is None:
                        raise AccountNotFoundError("account does not exist")
                    before = self._account(db, row)
                    if self._is_superadmin(before):
                        raise SuperadminImmutableError("superadmin accounts cannot be modified")
                    values = patch.model_dump(exclude_unset=True)
                    groups = before.groups if patch.group_keys is None else sorted(set(patch.group_keys))
                    self._validate_groups(db, groups)
                    level = before.permission_level if patch.permission_level is None else patch.permission_level
                    self._validate_level(level)
                    self._validate_superadmin_shape(level, groups)
                    enabled = before.enabled if patch.enabled is None else patch.enabled
                    old_viable = self._is_viable_superadmin(before.permission_level, before.enabled, before.groups)
                    new_viable = self._is_viable_superadmin(level, enabled, groups)
                    if old_viable and not new_viable and self._viable_superadmin_count(db) <= 1:
                        raise LastSuperadminError("the last enabled superadmin cannot be disabled or demoted")
                    assignments: list[str] = []
                    parameters: list[Any] = []
                    for field in ("display_name", "permission_level", "enabled", "must_change_password"):
                        if field in values and values[field] is not None:
                            assignments.append(f"{field} = ?")
                            parameters.append(
                                int(values[field]) if field in {"enabled", "must_change_password"} else values[field]
                            )
                    assignments.append("modified_at = ?")
                    parameters.append(_iso(_now()))
                    parameters.append(account_id)
                    db.execute(f"UPDATE webui_accounts SET {', '.join(assignments)} WHERE account_id = ?", parameters)
                    if level >= 1000:
                        db.execute(
                            "UPDATE webui_accounts SET bound_user_id = NULL WHERE account_id = ?",
                            (account_id,),
                        )
                        db.execute(
                            "DELETE FROM webui_account_user_bindings WHERE account_id = ?",
                            (account_id,),
                        )
                        db.execute(
                            "DELETE FROM webui_policy_bindings WHERE principal_type = 'account' "
                            "AND principal_id = ? AND managed_by = 'system.account_binding'",
                            (account_id,),
                        )
                    if patch.group_keys is not None:
                        db.execute("DELETE FROM webui_account_groups WHERE account_id = ?", (account_id,))
                        db.executemany(
                            "INSERT INTO webui_account_groups(account_id, group_key) VALUES (?, ?)",
                            [(account_id, group) for group in groups],
                        )
                    after_row = db.execute(
                        "SELECT * FROM webui_accounts WHERE account_id = ?", (account_id,)
                    ).fetchone()
                    after = self._account(db, after_row)
                    self._audit(
                        db,
                        actor_account_id,
                        "webui.account.update",
                        "account",
                        account_id,
                        self._safe_account_snapshot(before),
                        self._safe_account_snapshot(after),
                    )
                    db.commit()
                    return after
                except BaseException:
                    db.rollback()
                    raise

        return await asyncio.to_thread(write)

    async def update_own_profile(self, *, account_id: str, display_name: str) -> AccountRecord:
        """仅修改当前账号显示名称，超级管理员也可安全使用。"""

        await self.ensure()

        def write() -> AccountRecord:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute("SELECT * FROM webui_accounts WHERE account_id = ?", (account_id,)).fetchone()
                    if row is None:
                        raise AccountNotFoundError("account does not exist")
                    before = self._account(db, row)
                    normalized = display_name.strip()
                    if not normalized or len(normalized) > 128:
                        raise ValueError("display_name must contain 1 to 128 characters")
                    db.execute(
                        "UPDATE webui_accounts SET display_name = ?, modified_at = ? WHERE account_id = ?",
                        (normalized, _iso(_now()), account_id),
                    )
                    after_row = db.execute(
                        "SELECT * FROM webui_accounts WHERE account_id = ?", (account_id,)
                    ).fetchone()
                    after = self._account(db, after_row)
                    self._audit(
                        db,
                        account_id,
                        "webui.account.profile.update_self",
                        "account",
                        account_id,
                        {"display_name": before.display_name},
                        {"display_name": after.display_name},
                    )
                    db.commit()
                    return after
                except BaseException:
                    db.rollback()
                    raise

        return await asyncio.to_thread(write)

    async def create_session(self, account: AccountRecord, ttl: timedelta) -> tuple[str, datetime]:
        await self.ensure()
        token = new_session_token()
        expires_at = _now() + ttl

        def write() -> None:
            now = _iso(_now())
            with self._connect() as db:
                db.execute(
                    "INSERT INTO webui_sessions(session_id_hash, account_id, credential_version, expires_at, "
                    "created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        session_token_hash(token),
                        account.account_id,
                        account.credential_version,
                        _iso(expires_at),
                        now,
                        now,
                    ),
                )

        await asyncio.to_thread(write)
        return token, expires_at

    async def authenticate_session(self, token: str) -> AccountRecord | None:
        await self.ensure()

        def read() -> AccountRecord | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT a.* FROM webui_sessions s JOIN webui_accounts a ON a.account_id = s.account_id "
                    "WHERE s.session_id_hash = ? AND s.expires_at > ? AND s.credential_version = a.credential_version "
                    "AND a.enabled = 1",
                    (session_token_hash(token), _iso(_now())),
                ).fetchone()
                if row is None:
                    return None
                db.execute(
                    "UPDATE webui_sessions SET last_seen_at = ? WHERE session_id_hash = ?",
                    (_iso(_now()), session_token_hash(token)),
                )
                return self._account(db, row)

        return await asyncio.to_thread(read)

    async def revoke_session(self, token: str) -> None:
        await self.ensure()
        await asyncio.to_thread(self._delete_session_sync, token)

    def _delete_session_sync(self, token: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM webui_sessions WHERE session_id_hash = ?", (session_token_hash(token),))

    async def record_login_failure(self, account_id: str, *, threshold: int = 5, lock_minutes: int = 5) -> None:
        await self.ensure()

        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute(
                        "SELECT failed_login_count, locked_until FROM webui_accounts WHERE account_id = ?",
                        (account_id,),
                    ).fetchone()
                    if row is None:
                        db.commit()
                        return
                    now = _now()
                    current_lock = _datetime(row["locked_until"])
                    if current_lock is not None and current_lock > now:
                        db.commit()
                        return
                    failures = (0 if current_lock is not None else int(row["failed_login_count"])) + 1
                    locked_until = _iso(now + timedelta(minutes=lock_minutes)) if failures >= threshold else None
                    db.execute(
                        "UPDATE webui_accounts SET failed_login_count = ?, locked_until = ?, modified_at = ? "
                        "WHERE account_id = ?",
                        (failures, locked_until, _iso(now), account_id),
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)

    async def record_audit(
        self,
        *,
        actor_account_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        """记录不适合与账号事务绑定的 WebUI 审计事件。"""

        await self.ensure()

        def write() -> None:
            with self._connect() as db:
                self._audit(
                    db,
                    actor_account_id,
                    action,
                    resource_type,
                    resource_id,
                    before,
                    after,
                    request_id,
                )

        await asyncio.to_thread(write)

    async def list_audit_events(self, *, limit: int = 100, offset: int = 0) -> tuple[list[AuditEventResponse], int]:
        """按时间倒序读取审计事件，并返回未分页总数。"""

        await self.ensure()

        def read() -> tuple[list[AuditEventResponse], int]:
            with self._connect() as db:
                total = int(db.execute("SELECT COUNT(*) FROM webui_audit_events").fetchone()[0])
                rows = db.execute(
                    "SELECT event_id, actor_account_id, action, resource_type, resource_id, before_json, "
                    "after_json, request_id, created_at FROM webui_audit_events "
                    "ORDER BY created_at DESC, event_id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return (
                [
                    AuditEventResponse(
                        event_id=str(row["event_id"]),
                        actor_account_id=row["actor_account_id"],
                        action=str(row["action"]),
                        resource_type=str(row["resource_type"]),
                        resource_id=str(row["resource_id"]),
                        before=json.loads(row["before_json"]) if row["before_json"] else None,
                        after=json.loads(row["after_json"]) if row["after_json"] else None,
                        request_id=row["request_id"],
                        created_at=_datetime(row["created_at"]),
                    )
                    for row in rows
                ],
                total,
            )

        return await asyncio.to_thread(read)

    async def record_login_success(self, account_id: str) -> AccountRecord:
        await self.ensure()

        def write() -> AccountRecord:
            with self._connect() as db:
                now = _iso(_now())
                db.execute(
                    "UPDATE webui_accounts SET failed_login_count = 0, locked_until = NULL, last_login_at = ?, "
                    "modified_at = ? WHERE account_id = ?",
                    (now, now, account_id),
                )
            account = self._get_account_sync(account_id)
            if account is None:
                raise AccountNotFoundError("account does not exist")
            return account

        return await asyncio.to_thread(write)

    async def replace_password(
        self,
        *,
        account_id: str,
        password_hash: str,
        must_change_password: bool,
        actor_account_id: str,
        audit_action: str,
    ) -> None:
        await self.ensure()

        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    target_row = db.execute(
                        "SELECT * FROM webui_accounts WHERE account_id = ?", (account_id,)
                    ).fetchone()
                    if target_row is None:
                        raise AccountNotFoundError("account does not exist")
                    target = self._account(db, target_row)
                    if self._is_superadmin(target) and (
                        actor_account_id != account_id
                        or audit_action not in {"webui.account.password.change_self", "webui.account.password.rehash"}
                    ):
                        raise SuperadminImmutableError(
                            "superadmin passwords can only be changed through the self-service endpoint"
                        )
                    cursor = db.execute(
                        "UPDATE webui_accounts SET password_hash = ?, password_changed_at = ?, "
                        "must_change_password = ?, "
                        "credential_version = credential_version + 1, failed_login_count = 0, locked_until = NULL, "
                        "modified_at = ? WHERE account_id = ?",
                        (password_hash, _iso(_now()), int(must_change_password), _iso(_now()), account_id),
                    )
                    if cursor.rowcount != 1:
                        raise AccountNotFoundError("account does not exist")
                    db.execute("DELETE FROM webui_sessions WHERE account_id = ?", (account_id,))
                    self._audit(db, actor_account_id, audit_action, "account", account_id, None, None)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)

    def _get_account_sync(self, account_id: str) -> AccountRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM webui_accounts WHERE account_id = ?", (account_id,)).fetchone()
            return self._account(db, row) if row else None

    def _account(self, db: sqlite3.Connection, row: sqlite3.Row) -> AccountRecord:
        groups = [
            str(item[0])
            for item in db.execute(
                "SELECT group_key FROM webui_account_groups WHERE account_id = ? ORDER BY group_key",
                (row["account_id"],),
            ).fetchall()
        ]
        bound_user_ids = [
            str(item[0])
            for item in db.execute(
                "SELECT user_id FROM webui_account_user_bindings WHERE account_id = ? ORDER BY user_id",
                (row["account_id"],),
            ).fetchall()
        ]
        return AccountRecord(
            account_id=row["account_id"],
            login_name=row["login_name"],
            display_name=row["display_name"],
            password_hash=row["password_hash"],
            permission_level=row["permission_level"],
            enabled=bool(row["enabled"]),
            bound_user_id=row["bound_user_id"],
            must_change_password=bool(row["must_change_password"]),
            credential_version=row["credential_version"],
            failed_login_count=row["failed_login_count"],
            locked_until=_datetime(row["locked_until"]),
            password_changed_at=_datetime(row["password_changed_at"]),
            last_login_at=_datetime(row["last_login_at"]),
            created_at=_datetime(row["created_at"]),
            modified_at=_datetime(row["modified_at"]),
            groups=groups,
            bound_user_ids=bound_user_ids,
        )

    @staticmethod
    def _validate_groups(db: sqlite3.Connection, groups: list[str]) -> None:
        if not groups:
            return
        placeholders = ",".join("?" for _ in groups)
        found = {
            str(row[0])
            for row in db.execute(
                f"SELECT group_key FROM webui_permission_groups WHERE group_key IN ({placeholders})", groups
            ).fetchall()
        }
        unknown = sorted(set(groups) - found)
        if unknown:
            raise InvalidPermissionGroupError(f"unknown permission groups: {', '.join(unknown)}")

    @staticmethod
    def _group_permission_keys(db: sqlite3.Connection, group_key: str) -> list[str]:
        return [
            str(row[0])
            for row in db.execute(
                "SELECT permission_key FROM webui_group_permissions WHERE group_key = ? ORDER BY permission_key",
                (group_key,),
            ).fetchall()
        ]

    @classmethod
    def _permission_groups(cls, db: sqlite3.Connection) -> list[PermissionGroupResponse]:
        return [
            PermissionGroupResponse(
                key=str(row["group_key"]),
                name=str(row["name"]),
                description=str(row["description"]),
                permissions=cls._group_permission_keys(db, str(row["group_key"])),
                system_managed=bool(row["system_managed"]),
            )
            for row in db.execute(
                "SELECT group_key, name, description, system_managed FROM webui_permission_groups ORDER BY group_key"
            ).fetchall()
        ]

    def _get_permission_group_sync(self, group_key: str) -> PermissionGroupResponse:
        with self._connect() as db:
            row = db.execute(
                "SELECT group_key, name, description, system_managed FROM webui_permission_groups WHERE group_key = ?",
                (group_key,),
            ).fetchone()
            if row is None:
                raise PermissionGroupNotFoundError("permission group does not exist")
            return PermissionGroupResponse(
                key=str(row["group_key"]),
                name=str(row["name"]),
                description=str(row["description"]),
                permissions=self._group_permission_keys(db, group_key),
                system_managed=bool(row["system_managed"]),
            )

    @staticmethod
    def _validate_custom_permissions(db: sqlite3.Connection, permissions: list[str]) -> None:
        if not permissions:
            raise InvalidPermissionGroupError("a custom permission group requires at least one permission")
        placeholders = ",".join("?" for _ in permissions)
        found = {
            str(row[0])
            for row in db.execute(
                f"SELECT permission_key FROM webui_permission_nodes WHERE permission_key IN ({placeholders}) "
                "AND available = 1 AND custom_assignable = 1",
                permissions,
            ).fetchall()
        }
        invalid = sorted(set(permissions) - found)
        if invalid:
            raise InvalidPermissionGroupError(
                f"permissions are unavailable or not custom-assignable: {', '.join(invalid)}"
            )

    @staticmethod
    def _is_viable_superadmin(level: int, enabled: bool, groups: list[str]) -> bool:
        del groups
        return enabled and level >= 1000

    @staticmethod
    def _is_superadmin(account: AccountRecord) -> bool:
        return account.permission_level == 1000

    @staticmethod
    def _viable_superadmin_count(db: sqlite3.Connection) -> int:
        row = db.execute(
            "SELECT COUNT(*) FROM webui_accounts WHERE enabled = 1 AND permission_level >= 1000"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _safe_account_snapshot(account: AccountRecord) -> dict[str, Any]:
        return account.model_dump(
            mode="json", exclude={"password_hash", "credential_version", "failed_login_count", "locked_until"}
        )

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        actor_account_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        request_id: str | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO webui_audit_events(event_id, actor_account_id, action, resource_type, resource_id, "
            "before_json, after_json, request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                actor_account_id,
                action,
                resource_type,
                resource_id,
                json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
                json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None,
                request_id or current_audit_request_id(),
                _iso(_now()),
            ),
        )
