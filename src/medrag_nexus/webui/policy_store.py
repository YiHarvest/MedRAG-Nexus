"""保存权限感知 WebUI BFF 的资源策略、ACL 与生命周期状态。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from .models import AccountRecord

PolicyAction = str
ResourceType = Literal["user", "workspace"]
LifecycleState = Literal["active", "deleting", "delete_failed", "deleted"]

USER_POLICY_ACTIONS = (
    "webui.user.read",
    "webui.workspace.create",
    "webui.user.rename",
    "webui.user.delete",
    "webui.user.policy.manage",
)
WORKSPACE_POLICY_ACTIONS = (
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class UserPolicy:
    user_id: str
    read_min_level: int = 0
    workspace_create_min_level: int = 1000
    policy_version: int = 0


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    workspace_id: str
    read_min_level: int = 0
    cud_min_level: int = 1000
    policy_version: int = 0


@dataclass(frozen=True, slots=True)
class PolicyBinding:
    principal_type: Literal["account", "group"]
    principal_id: str
    effect: Literal["allow", "deny"]
    immutable: bool = False
    managed_by: str | None = None


@dataclass(frozen=True, slots=True)
class PendingDeletion:
    workspace_id: str
    state: Literal["deleting", "delete_failed"]
    detail: str | None
    modified_at: str


class InvalidPolicyBindingError(ValueError):
    """ACL 绑定中的动作、主体或资源类型无效。"""


class KnowledgePolicyStore:
    """读写 WebUI 旁路策略，不修改公共 API 数据结构。"""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    async def ensure(self) -> None:
        def create() -> None:
            with self._connect() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS webui_workspace_lifecycle (
                        workspace_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL CHECK(state IN ('active', 'deleting', 'delete_failed', 'deleted')),
                        detail TEXT,
                        modified_by_account_id TEXT,
                        modified_at TEXT NOT NULL
                    );
                    """
                )
                self._ensure_v2_bindings(db)

        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(create)

    @staticmethod
    def _ensure_v2_bindings(db: sqlite3.Connection) -> None:
        """把旧的 read/cud ACL 表升级成使用完整权限节点的 V2 表。"""

        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'webui_policy_bindings'"
        ).fetchone()
        if row is None:
            KnowledgePolicyStore._create_v2_bindings(db)
            return
        sql = str(row[0] or "")
        columns = {str(item[1]) for item in db.execute("PRAGMA table_info(webui_policy_bindings)")}
        if (
            "immutable" in columns
            and "managed_by" in columns
            and "'level'" not in sql
            and "action TEXT NOT NULL" in sql
            and "action IN" not in sql
        ):
            return

        db.execute("DROP INDEX IF EXISTS idx_webui_policy_resource")
        db.execute("ALTER TABLE webui_policy_bindings RENAME TO webui_policy_bindings_legacy")
        KnowledgePolicyStore._create_v2_bindings(db)
        legacy_columns = {
            str(item[1]) for item in db.execute("PRAGMA table_info(webui_policy_bindings_legacy)")
        }
        rows = db.execute("SELECT * FROM webui_policy_bindings_legacy").fetchall()
        workspace_writes = WORKSPACE_POLICY_ACTIONS[1:]
        for item in rows:
            if str(item["principal_type"]) not in {"account", "group"}:
                continue
            resource_type = str(item["resource_type"])
            old_action = str(item["action"])
            if old_action == "read":
                actions = (
                    "webui.user.read" if resource_type == "user" else "webui.workspace.read",
                )
            elif old_action == "create_workspace":
                actions = ("webui.workspace.create",)
            elif old_action == "cud" and resource_type == "workspace":
                actions = workspace_writes
            else:
                actions = (old_action,)
            for action in actions:
                db.execute(
                    "INSERT OR IGNORE INTO webui_policy_bindings(binding_id, resource_type, resource_id, "
                    "action, principal_type, principal_id, effect, immutable, managed_by, "
                    "created_by_account_id, created_at) "
                    "VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        resource_type,
                        str(item["resource_id"]),
                        action,
                        str(item["principal_type"]),
                        str(item["principal_id"]),
                        str(item["effect"]),
                        int(item["immutable"])
                        if "immutable" in legacy_columns
                        else int(item["system_managed"])
                        if "system_managed" in legacy_columns
                        else 0,
                        item["managed_by"]
                        if "managed_by" in legacy_columns
                        else item["system_source"]
                        if "system_source" in legacy_columns
                        else None,
                        item["created_by_account_id"],
                        str(item["created_at"]),
                    ),
                )
        db.execute("DROP TABLE webui_policy_bindings_legacy")

    @staticmethod
    def _create_v2_bindings(db: sqlite3.Connection) -> None:
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
            CREATE INDEX IF NOT EXISTS idx_webui_policy_resource
                ON webui_policy_bindings(resource_type, resource_id, action);
            """
        )

    async def get_user_policy(self, user_id: str) -> UserPolicy:
        def read() -> UserPolicy:
            with self._connect() as db:
                row = db.execute(
                    "SELECT user_id, read_min_level, workspace_create_min_level, policy_version "
                    "FROM webui_user_policies WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                return UserPolicy(**dict(row)) if row else UserPolicy(user_id=user_id)

        return await asyncio.to_thread(read)

    async def set_user_policy(
        self,
        user_id: str,
        *,
        read_min_level: int,
        workspace_create_min_level: int,
        actor_account_id: str,
    ) -> UserPolicy:
        def write() -> UserPolicy:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO webui_user_policies(user_id, read_min_level, workspace_create_min_level, "
                    "policy_version, modified_by_account_id, modified_at) VALUES (?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET read_min_level=excluded.read_min_level, "
                    "workspace_create_min_level=excluded.workspace_create_min_level, "
                    "policy_version=webui_user_policies.policy_version + 1, "
                    "modified_by_account_id=excluded.modified_by_account_id, modified_at=excluded.modified_at",
                    (user_id, read_min_level, workspace_create_min_level, actor_account_id, _now()),
                )
                row = db.execute(
                    "SELECT user_id, read_min_level, workspace_create_min_level, policy_version "
                    "FROM webui_user_policies WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                return UserPolicy(**dict(row))

        return await asyncio.to_thread(write)

    async def get_workspace_policy(self, workspace_id: str) -> WorkspacePolicy:
        def read() -> WorkspacePolicy:
            with self._connect() as db:
                row = db.execute(
                    "SELECT workspace_id, read_min_level, cud_min_level, policy_version "
                    "FROM webui_workspace_policies WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                return WorkspacePolicy(**dict(row)) if row else WorkspacePolicy(workspace_id=workspace_id)

        return await asyncio.to_thread(read)

    async def set_workspace_policy(
        self,
        workspace_id: str,
        *,
        read_min_level: int,
        cud_min_level: int,
        actor_account_id: str,
        creating: bool = False,
    ) -> WorkspacePolicy:
        def write() -> WorkspacePolicy:
            now = _now()
            with self._connect() as db:
                db.execute(
                    "INSERT INTO webui_workspace_policies(workspace_id, read_min_level, cud_min_level, "
                    "policy_version, created_by_account_id, modified_by_account_id, created_at, modified_at) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?, ?) ON CONFLICT(workspace_id) DO UPDATE SET "
                    "read_min_level=excluded.read_min_level, cud_min_level=excluded.cud_min_level, "
                    "policy_version=webui_workspace_policies.policy_version + 1, "
                    "modified_by_account_id=excluded.modified_by_account_id, modified_at=excluded.modified_at",
                    (
                        workspace_id,
                        read_min_level,
                        cud_min_level,
                        actor_account_id if creating else None,
                        actor_account_id,
                        now,
                        now,
                    ),
                )
                row = db.execute(
                    "SELECT workspace_id, read_min_level, cud_min_level, policy_version "
                    "FROM webui_workspace_policies WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                return WorkspacePolicy(**dict(row))

        return await asyncio.to_thread(write)

    async def mark_lifecycle(
        self,
        workspace_id: str,
        state: LifecycleState,
        *,
        actor_account_id: str,
        detail: str | None = None,
    ) -> None:
        def write() -> None:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO webui_workspace_lifecycle(workspace_id, state, detail, "
                    "modified_by_account_id, modified_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET state=excluded.state, detail=excluded.detail, "
                    "modified_by_account_id=excluded.modified_by_account_id, modified_at=excluded.modified_at",
                    (workspace_id, state, detail, actor_account_id, _now()),
                )

        await asyncio.to_thread(write)

    async def lifecycle(self, workspace_id: str) -> LifecycleState | None:
        def read() -> LifecycleState | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT state FROM webui_workspace_lifecycle WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                return str(row[0]) if row else None  # type: ignore[return-value]

        return await asyncio.to_thread(read)

    async def failed_deletions(self) -> list[tuple[str, str | None]]:
        def read() -> list[tuple[str, str | None]]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT workspace_id, detail FROM webui_workspace_lifecycle "
                    "WHERE state = 'delete_failed' ORDER BY modified_at"
                ).fetchall()
                return [(str(row["workspace_id"]), row["detail"]) for row in rows]

        return await asyncio.to_thread(read)

    async def pending_deletions(self, *, deleting_lease_seconds: int) -> list[PendingDeletion]:
        """返回失败删除，以及租约已经过期的进行中删除。"""

        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=deleting_lease_seconds)).isoformat()

        def read() -> list[PendingDeletion]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT workspace_id, state, detail, modified_at FROM webui_workspace_lifecycle "
                    "WHERE state = 'delete_failed' OR (state = 'deleting' AND modified_at <= ?) "
                    "ORDER BY modified_at",
                    (stale_before,),
                ).fetchall()
                return [
                    PendingDeletion(
                        workspace_id=str(row["workspace_id"]),
                        state=str(row["state"]),  # type: ignore[arg-type]
                        detail=row["detail"],
                        modified_at=str(row["modified_at"]),
                    )
                    for row in rows
                ]

        return await asyncio.to_thread(read)

    async def delete_workspace_policy_data(self, workspace_id: str) -> None:
        """删除 Workspace 的策略和 ACL，仅保留生命周期墓碑与审计记录。"""

        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute(
                        "DELETE FROM webui_policy_bindings WHERE resource_type = 'workspace' AND resource_id = ?",
                        (workspace_id,),
                    )
                    db.execute("DELETE FROM webui_workspace_policies WHERE workspace_id = ?", (workspace_id,))
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)

    async def delete_user_policy_data(self, user_id: str) -> None:
        """删除知识用户的等级策略与全部 ACL。"""

        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute(
                        "DELETE FROM webui_policy_bindings WHERE resource_type = 'user' AND resource_id = ?",
                        (user_id,),
                    )
                    db.execute("DELETE FROM webui_user_policies WHERE user_id = ?", (user_id,))
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)

    async def list_bindings(
        self,
        resource_type: ResourceType,
        resource_id: str,
        actions: Iterable[PolicyAction],
    ) -> dict[PolicyAction, list[PolicyBinding]]:
        """按动作返回资源的完整 ACL，未设置的动作返回空列表。"""

        requested_actions = tuple(actions)

        def read() -> dict[PolicyAction, list[PolicyBinding]]:
            result = {action: [] for action in requested_actions}
            with self._connect() as db:
                rows = db.execute(
                    "SELECT action, principal_type, principal_id, effect, immutable, managed_by "
                    "FROM webui_policy_bindings "
                    "WHERE resource_type = ? AND resource_id = ? ORDER BY action, principal_type, principal_id",
                    (resource_type, resource_id),
                ).fetchall()
            for row in rows:
                action = str(row["action"])
                if action in result:
                    result[action].append(
                        PolicyBinding(
                            principal_type=str(row["principal_type"]),  # type: ignore[arg-type]
                            principal_id=str(row["principal_id"]),
                            effect=str(row["effect"]),  # type: ignore[arg-type]
                            immutable=bool(row["immutable"]),
                            managed_by=row["managed_by"],
                        )
                    )
            return result

        return await asyncio.to_thread(read)

    async def replace_bindings(
        self,
        resource_type: ResourceType,
        resource_id: str,
        action: PolicyAction,
        bindings: list[PolicyBinding],
        *,
        actor_account_id: str,
    ) -> dict[PolicyAction, list[PolicyBinding]]:
        """原子替换单个动作的 ACL，并校验账号、权限组与重复主体。"""

        allowed_actions: dict[ResourceType, tuple[PolicyAction, ...]] = {
            "user": USER_POLICY_ACTIONS,
            "workspace": WORKSPACE_POLICY_ACTIONS,
        }
        if resource_type not in allowed_actions or action not in allowed_actions[resource_type]:
            raise InvalidPolicyBindingError("resource type and action do not match")
        principal_keys = [(binding.principal_type, binding.principal_id) for binding in bindings]
        if len(principal_keys) != len(set(principal_keys)):
            raise InvalidPolicyBindingError("duplicate principal in bindings")

        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    for binding in bindings:
                        system_row = db.execute(
                            "SELECT 1 FROM webui_policy_bindings WHERE resource_type = ? AND resource_id = ? "
                            "AND action = ? AND principal_type = ? AND principal_id = ? AND immutable = 1",
                            (
                                resource_type,
                                resource_id,
                                action,
                                binding.principal_type,
                                binding.principal_id,
                            ),
                        ).fetchone()
                        if system_row is not None:
                            raise InvalidPolicyBindingError("the selected ACL principal is managed by the system")
                        if binding.principal_type == "account":
                            account_row = db.execute(
                                "SELECT 1 FROM webui_accounts WHERE account_id = ?", (binding.principal_id,)
                            ).fetchone()
                            exists = account_row
                            if binding.effect == "deny" and self._is_superadmin_account(db, binding.principal_id):
                                raise InvalidPolicyBindingError("superadmin ACL cannot be changed")
                        elif binding.principal_type == "group":
                            exists = db.execute(
                                "SELECT 1 FROM webui_permission_groups WHERE group_key = ?", (binding.principal_id,)
                            ).fetchone()
                            if binding.effect == "deny" and self._group_contains_superadmin(db, binding.principal_id):
                                raise InvalidPolicyBindingError("an ACL deny cannot include a superadmin")
                        else:  # pragma: no cover - 类型与接口模型已限制主体类型
                            raise InvalidPolicyBindingError("principal type must be account or group")
                        if exists is None:
                            raise InvalidPolicyBindingError(
                                f"unknown {binding.principal_type} principal: {binding.principal_id}"
                            )
                    db.execute(
                        "DELETE FROM webui_policy_bindings WHERE resource_type = ? AND resource_id = ? AND action = ? "
                        "AND immutable = 0",
                        (resource_type, resource_id, action),
                    )
                    now = _now()
                    db.executemany(
                        "INSERT INTO webui_policy_bindings(binding_id, resource_type, resource_id, action, "
                        "principal_type, principal_id, effect, immutable, managed_by, "
                        "created_by_account_id, created_at) "
                        "VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)",
                        [
                            (
                                resource_type,
                                resource_id,
                                action,
                                binding.principal_type,
                                binding.principal_id,
                                binding.effect,
                                actor_account_id,
                                now,
                            )
                            for binding in bindings
                        ],
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)
        return await self.list_bindings(resource_type, resource_id, allowed_actions[resource_type])

    async def ensure_resource_acl(
        self,
        resource_type: ResourceType,
        resource_id: str,
        *,
        user_id: str | None = None,
    ) -> None:
        """为每个超级管理员和已绑定普通账号补齐不可编辑的系统 ACL。"""

        actions = USER_POLICY_ACTIONS if resource_type == "user" else WORKSPACE_POLICY_ACTIONS

        def write() -> None:
            now = _now()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    superadmins = db.execute(
                        "SELECT account_id FROM webui_accounts "
                        "WHERE enabled = 1 AND permission_level = 1000"
                    ).fetchall()
                    for row in superadmins:
                        self._insert_system_allows(
                            db,
                            resource_type,
                            resource_id,
                            actions,
                            principal_type="account",
                            principal_id=str(row["account_id"]),
                            source="system.superadmin",
                            now=now,
                        )
                    owner_user_id = resource_id if resource_type == "user" else user_id
                    if owner_user_id:
                        rows = db.execute(
                            "SELECT account_id FROM webui_account_user_bindings WHERE user_id = ?",
                            (owner_user_id,),
                        ).fetchall()
                        for row in rows:
                            self._insert_system_allows(
                                db,
                                resource_type,
                                resource_id,
                                actions,
                                principal_type="account",
                                principal_id=str(row["account_id"]),
                                source="system.account_binding",
                                now=now,
                            )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)

    async def replace_bound_account_access(
        self,
        account_id: str,
        *,
        old_user_id: str | None,
        new_user_id: str | None,
        workspace_ids: Iterable[str] = (),
    ) -> None:
        """兼容单知识域调用方，并转交多绑定 ACL 更新。"""

        bindings = {new_user_id: tuple(workspace_ids)} if new_user_id else {}
        del old_user_id
        await self.replace_account_binding_access(account_id, bindings)

    async def replace_account_binding_access(
        self,
        account_id: str,
        bindings: Mapping[str, Iterable[str]],
    ) -> None:
        """按普通账号的全部知识域绑定重建系统管理 ACL。"""

        normalized = {
            user_id: tuple(dict.fromkeys(workspace_ids))
            for user_id, workspace_ids in bindings.items()
        }

        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute(
                        "DELETE FROM webui_policy_bindings WHERE principal_type = 'account' "
                        "AND principal_id = ? AND immutable = 1 AND managed_by = 'system.account_binding'",
                        (account_id,),
                    )
                    now = _now()
                    for user_id, ids in normalized.items():
                        self._insert_system_allows(
                            db,
                            "user",
                            user_id,
                            USER_POLICY_ACTIONS,
                            principal_type="account",
                            principal_id=account_id,
                            source="system.account_binding",
                            now=now,
                        )
                        for workspace_id in ids:
                            self._insert_system_allows(
                                db,
                                "workspace",
                                workspace_id,
                                WORKSPACE_POLICY_ACTIONS,
                                principal_type="account",
                                principal_id=account_id,
                                source="system.account_binding",
                                now=now,
                            )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)

    async def grant_workspace_creator_access(self, account_id: str, workspace_id: str) -> None:
        """让 Workspace 创建者可继续读取、配置和维护其新建资源。"""

        def write() -> None:
            with self._connect() as db:
                self._insert_system_allows(
                    db,
                    "workspace",
                    workspace_id,
                    WORKSPACE_POLICY_ACTIONS,
                    principal_type="account",
                    principal_id=account_id,
                    source="system.workspace_creator",
                    now=_now(),
                )

        await asyncio.to_thread(write)

    async def grant_user_creator_access(self, account_id: str, user_id: str) -> None:
        """让知识域创建者继续管理其刚创建的知识域。"""

        def write() -> None:
            with self._connect() as db:
                self._insert_system_allows(
                    db,
                    "user",
                    user_id,
                    USER_POLICY_ACTIONS,
                    principal_type="account",
                    principal_id=account_id,
                    source="system.user_creator",
                    now=_now(),
                )

        await asyncio.to_thread(write)

    async def leave_resource_access(
        self,
        account_id: str,
        *,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        """撤销账号在资源上的全部直接授权，并用个人拒绝覆盖权限组授权。"""

        await self.ensure()
        actions = USER_POLICY_ACTIONS if resource_type == "user" else WORKSPACE_POLICY_ACTIONS

        def write() -> None:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    if not db.execute(
                        "SELECT 1 FROM webui_accounts WHERE account_id = ?", (account_id,)
                    ).fetchone():
                        raise InvalidPolicyBindingError("account does not exist")
                    if self._is_superadmin_account(db, account_id):
                        raise InvalidPolicyBindingError("superadmin resource access cannot be removed")
                    placeholders = ",".join("?" for _ in actions)
                    db.execute(
                        "DELETE FROM webui_policy_bindings WHERE resource_type = ? AND resource_id = ? "
                        f"AND action IN ({placeholders}) AND principal_type = 'account' AND principal_id = ?",
                        (resource_type, resource_id, *actions, account_id),
                    )
                    now = _now()
                    db.executemany(
                        "INSERT INTO webui_policy_bindings(binding_id, resource_type, resource_id, action, "
                        "principal_type, principal_id, effect, immutable, managed_by, "
                        "created_by_account_id, created_at) "
                        "VALUES (lower(hex(randomblob(16))), ?, ?, ?, 'account', ?, 'deny', 0, "
                        "'self.leave', ?, ?)",
                        [
                            (resource_type, resource_id, action, account_id, account_id, now)
                            for action in actions
                        ],
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

        await asyncio.to_thread(write)

    async def allows_user(
        self,
        account: AccountRecord,
        permissions: set[str],
        *,
        user_id: str,
        action: PolicyAction,
        permission: str,
    ) -> bool:
        if permission != action or permission not in permissions or action not in USER_POLICY_ACTIONS:
            return False
        await self.ensure_resource_acl("user", user_id)
        policy = await self.get_user_policy(user_id)
        minimum = (
            policy.read_min_level
            if action in {"webui.user.read", "webui.user.policy.manage"}
            else policy.workspace_create_min_level
        )
        effect = await asyncio.to_thread(self._binding_effect, account, "user", user_id, action)
        return account.permission_level >= minimum and effect == "allow"

    async def allows_workspace(
        self,
        account: AccountRecord,
        permissions: set[str],
        *,
        workspace_id: str,
        action: PolicyAction,
        permission: str,
        user_id: str | None = None,
    ) -> bool:
        if permission != action or permission not in permissions or action not in WORKSPACE_POLICY_ACTIONS:
            return False
        if await self.lifecycle(workspace_id) in {"deleting", "delete_failed", "deleted"}:
            return False
        await self.ensure_resource_acl("workspace", workspace_id, user_id=user_id)
        policy = await self.get_workspace_policy(workspace_id)
        minimum = (
            policy.read_min_level
            if action in {"webui.workspace.read", "webui.resource.file.download"}
            else policy.cud_min_level
        )
        effect = await asyncio.to_thread(self._binding_effect, account, "workspace", workspace_id, action)
        return account.permission_level >= minimum and effect == "allow"

    def _binding_effect(
        self,
        account: AccountRecord,
        resource_type: Literal["user", "workspace"],
        resource_id: str,
        action: PolicyAction,
    ) -> Literal["allow", "deny"] | None:
        with self._connect() as db:
            rows = db.execute(
                "SELECT principal_type, principal_id, effect FROM webui_policy_bindings "
                "WHERE resource_type = ? AND resource_id = ? AND action = ?",
                (resource_type, resource_id, action),
            ).fetchall()
        if not rows:
            return None
        matching = [
            row
            for row in rows
            if (row["principal_type"] == "account" and row["principal_id"] == account.account_id)
            or (row["principal_type"] == "group" and row["principal_id"] in account.groups)
        ]
        if any(row["effect"] == "deny" for row in matching):
            return "deny"
        if any(row["effect"] == "allow" for row in matching):
            return "allow"
        return None

    @staticmethod
    def _is_superadmin_account(db: sqlite3.Connection, account_id: str) -> bool:
        return bool(
            db.execute(
                "SELECT 1 FROM webui_accounts WHERE account_id = ? "
                "AND enabled = 1 AND permission_level >= 1000",
                (account_id,),
            ).fetchone()
        )

    @staticmethod
    def _group_contains_superadmin(db: sqlite3.Connection, group_key: str) -> bool:
        return bool(
            db.execute(
                "SELECT 1 FROM webui_account_groups target "
                "JOIN webui_accounts a ON a.account_id = target.account_id "
                "WHERE target.group_key = ? AND a.enabled = 1 AND a.permission_level >= 1000 LIMIT 1",
                (group_key,),
            ).fetchone()
        )

    @staticmethod
    def _insert_system_allows(
        db: sqlite3.Connection,
        resource_type: ResourceType,
        resource_id: str,
        actions: Iterable[PolicyAction],
        *,
        principal_type: Literal["account", "group"],
        principal_id: str,
        source: str,
        now: str,
    ) -> None:
        db.executemany(
            "INSERT OR IGNORE INTO webui_policy_bindings(binding_id, resource_type, resource_id, action, "
            "principal_type, principal_id, effect, immutable, managed_by, created_by_account_id, created_at) "
            "VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?, ?, 'allow', 1, ?, NULL, ?)",
            [
                (resource_type, resource_id, action, principal_type, principal_id, source, now)
                for action in actions
            ],
        )
