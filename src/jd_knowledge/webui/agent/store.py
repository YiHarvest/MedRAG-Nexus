"""使用 SQLite 持久化账号绑定的 Agent 操作与临时制品。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    ActionRiskLevel,
    ActionStatus,
    ActionTarget,
    AgentAction,
    AgentArtifact,
    ArtifactDownloadRecord,
    ArtifactResourceRequirement,
    ConfirmationMode,
)


class AgentStoreError(RuntimeError):
    code = "agent_store_error"


class ActionNotFoundError(AgentStoreError):
    code = "action_not_found"


class ActionOwnershipError(AgentStoreError):
    code = "action_account_mismatch"


class ActionStateError(AgentStoreError):
    code = "invalid_action_state"


class ActionPayloadError(AgentStoreError):
    code = "unsafe_action_payload"


class InvalidConfirmationError(AgentStoreError):
    code = "invalid_confirmation"


class IdempotencyConflictError(AgentStoreError):
    code = "idempotency_conflict"


class ArtifactNotFoundError(AgentStoreError):
    code = "artifact_not_found"


class ArtifactUnavailableError(AgentStoreError):
    code = "artifact_unavailable"


_SECRET_KEYS = {
    "password",
    "current_password",
    "new_password",
    "password_hash",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "file_bytes",
    "content_bytes",
    "content_base64",
    "file_base64",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _required_datetime(value: str | None, field_name: str) -> datetime:
    """解析数据库中的必填时间；数据损坏时给出明确错误。"""

    parsed = _datetime(value)
    if parsed is None:
        raise AgentStoreError(f"required timestamp is missing: {field_name}")
    return parsed


def _reject_sensitive(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            password_flag = normalized == "must_change_password" and isinstance(nested, bool)
            numeric_byte_count = (
                normalized.endswith("_bytes")
                and isinstance(nested, int)
                and not isinstance(nested, bool)
                and nested >= 0
            )
            if (
                normalized in _SECRET_KEYS
                or ("password" in normalized and not password_flag)
                or (normalized.endswith("_bytes") and not numeric_byte_count)
            ):
                location = ".".join((*path, str(key)))
                raise ActionPayloadError(f"sensitive field cannot be persisted in action arguments: {location}")
            _reject_sensitive(nested, (*path, str(key)))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive(nested, (*path, str(index)))
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise ActionPayloadError("binary values cannot be persisted in action arguments")


def _canonicalize_arguments(arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """校验操作参数，并按确定的键顺序序列化。"""

    plain_arguments = dict(arguments)
    _reject_sensitive(plain_arguments)
    try:
        encoded = json.dumps(
            plain_arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ActionPayloadError("action arguments must be finite JSON values") from exc
    if not isinstance(decoded, dict):
        raise ActionPayloadError("action arguments must be an object")
    return decoded, encoded


def canonical_arguments(arguments: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """校验确定性且可安全持久化参数的公开入口。"""

    return _canonicalize_arguments(arguments)


class AgentStore:
    """管理 WebUI SQLite 数据库中新增的 ``webui_agent_*`` 表。"""

    def __init__(self, path: Path):
        self.path = path
        self._ensured = False
        self._ensure_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA busy_timeout = 30000")
        return database

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
        with self._connect() as database:
            database.execute("PRAGMA journal_mode = WAL")
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS webui_agent_actions (
                    action_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    canonical_arguments_json TEXT NOT NULL,
                    required_permissions_json TEXT NOT NULL,
                    target_json TEXT,
                    risk_level TEXT NOT NULL,
                    confirmation_mode TEXT NOT NULL DEFAULT 'click',
                    status TEXT NOT NULL,
                    idempotency_key TEXT,
                    result_summary_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    modified_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    executing_at TEXT,
                    completed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_webui_agent_action_idempotency
                    ON webui_agent_actions(account_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_webui_agent_action_account
                    ON webui_agent_actions(account_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_webui_agent_action_expiry
                    ON webui_agent_actions(status, expires_at);

                CREATE TABLE IF NOT EXISTS webui_agent_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    owner_account_id TEXT NOT NULL,
                    conversation_id TEXT,
                    message_id TEXT,
                    file_name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoked_by_account_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_webui_agent_artifact_expiry
                    ON webui_agent_artifacts(expires_at);
                CREATE TABLE IF NOT EXISTS webui_agent_artifact_permissions (
                    artifact_id TEXT NOT NULL REFERENCES webui_agent_artifacts(artifact_id) ON DELETE CASCADE,
                    permission_key TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, permission_key)
                );
                CREATE TABLE IF NOT EXISTS webui_agent_artifact_resources (
                    artifact_id TEXT NOT NULL REFERENCES webui_agent_artifacts(artifact_id) ON DELETE CASCADE,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    required_permission TEXT NOT NULL,
                    PRIMARY KEY (artifact_id, resource_type, resource_id, required_permission)
                );
                CREATE TABLE IF NOT EXISTS webui_agent_artifact_downloads (
                    download_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL REFERENCES webui_agent_artifacts(artifact_id) ON DELETE CASCADE,
                    account_id TEXT NOT NULL,
                    downloaded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_webui_agent_artifact_download
                    ON webui_agent_artifact_downloads(artifact_id, downloaded_at DESC);
                """
            )
            columns = {str(row[1]) for row in database.execute("PRAGMA table_info(webui_agent_actions)")}
            if "confirmation_mode" not in columns:
                database.execute(
                    "ALTER TABLE webui_agent_actions ADD COLUMN confirmation_mode TEXT NOT NULL DEFAULT 'click'"
                )

    async def create_action(
        self,
        *,
        account_id: str,
        conversation_id: str,
        tool_name: str,
        canonical_arguments: Mapping[str, Any],
        required_permissions: Iterable[str] = (),
        target: ActionTarget | Mapping[str, Any] | None = None,
        risk_level: ActionRiskLevel = "sensitive",
        confirmation_mode: ConfirmationMode = "click",
        ttl: timedelta = timedelta(minutes=15),
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> AgentAction:
        await self.ensure()
        if ttl <= timedelta(0):
            raise ValueError("action ttl must be positive")
        safe_arguments, arguments_json = _canonicalize_arguments(canonical_arguments)
        permission_list = sorted(set(required_permissions))
        created_at = now or utc_now()
        action_id = uuid4().hex
        normalized_target = ActionTarget.model_validate(target) if target is not None else None
        target_json = normalized_target.model_dump_json() if normalized_target else None

        def create() -> AgentAction:
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                if idempotency_key is not None:
                    existing = database.execute(
                        "SELECT * FROM webui_agent_actions WHERE account_id = ? AND idempotency_key = ?",
                        (account_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if not self._same_action_request(
                            existing,
                            conversation_id=conversation_id,
                            tool_name=tool_name,
                            arguments_json=arguments_json,
                            required_permissions=permission_list,
                            target_json=target_json,
                            risk_level=risk_level,
                            confirmation_mode=confirmation_mode,
                        ):
                            raise IdempotencyConflictError("idempotency key was already used for another action")
                        return self._action(existing)
                database.execute(
                    """
                    INSERT INTO webui_agent_actions(
                        action_id, account_id, conversation_id, tool_name, canonical_arguments_json,
                        required_permissions_json, target_json, risk_level, confirmation_mode, status, idempotency_key,
                        created_at, modified_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        account_id,
                        conversation_id,
                        tool_name,
                        arguments_json,
                        json.dumps(permission_list, ensure_ascii=False, separators=(",", ":")),
                        target_json,
                        risk_level,
                        confirmation_mode,
                        idempotency_key,
                        _iso(created_at),
                        _iso(created_at),
                        _iso(created_at + ttl),
                    ),
                )
                row = database.execute("SELECT * FROM webui_agent_actions WHERE action_id = ?", (action_id,)).fetchone()
                assert row is not None
                return self._action(row, arguments=safe_arguments)

        return await asyncio.to_thread(create)

    @staticmethod
    def _same_action_request(
        row: sqlite3.Row,
        *,
        conversation_id: str,
        tool_name: str,
        arguments_json: str,
        required_permissions: list[str],
        target_json: str | None,
        risk_level: str,
        confirmation_mode: str,
    ) -> bool:
        return (
            row["conversation_id"] == conversation_id
            and row["tool_name"] == tool_name
            and row["canonical_arguments_json"] == arguments_json
            and json.loads(row["required_permissions_json"]) == required_permissions
            and row["target_json"] == target_json
            and row["risk_level"] == risk_level
            and row["confirmation_mode"] == confirmation_mode
        )

    async def get_action(self, action_id: str, *, account_id: str, now: datetime | None = None) -> AgentAction:
        """按所属账号读取操作，并惰性过期尚未提交的操作。"""

        await self.ensure()
        timestamp = now or utc_now()

        def read() -> AgentAction:
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                row = self._owned_action_row(database, action_id, account_id)
                row = self._expire_row_if_due(database, row, timestamp)
                return self._action(row)

        return await asyncio.to_thread(read)

    async def confirm_action(
        self,
        action_id: str,
        *,
        account_id: str,
        confirmation_text: str | None = None,
        now: datetime | None = None,
    ) -> AgentAction:
        action = await self.get_action(action_id, account_id=account_id, now=now)
        if action.confirmation_mode == "typed_text":
            expected = action.target.display_name if action.target else None
            if expected is None or confirmation_text != expected:
                raise InvalidConfirmationError("confirmation text does not match the action target")
        return await self._transition(action_id, account_id, "confirmed", {"pending"}, now=now)

    async def start_action(self, action_id: str, *, account_id: str, now: datetime | None = None) -> AgentAction:
        return await self._transition(action_id, account_id, "executing", {"confirmed"}, now=now)

    async def succeed_action(
        self,
        action_id: str,
        *,
        account_id: str,
        result_summary: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> AgentAction:
        summary_json = self._safe_summary(result_summary)
        return await self._transition(
            action_id,
            account_id,
            "succeeded",
            {"executing"},
            now=now,
            result_summary_json=summary_json,
        )

    async def fail_action(
        self,
        action_id: str,
        *,
        account_id: str,
        error: str,
        result_summary: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> AgentAction:
        summary_json = self._safe_summary(result_summary)
        return await self._transition(
            action_id,
            account_id,
            "failed",
            {"executing"},
            now=now,
            result_summary_json=summary_json,
            error=error[:2000],
        )

    async def cancel_action(self, action_id: str, *, account_id: str, now: datetime | None = None) -> AgentAction:
        return await self._transition(action_id, account_id, "cancelled", {"pending", "confirmed"}, now=now)

    async def _transition(
        self,
        action_id: str,
        account_id: str,
        destination: ActionStatus,
        allowed_sources: set[ActionStatus],
        *,
        now: datetime | None,
        result_summary_json: str | None = None,
        error: str | None = None,
    ) -> AgentAction:
        await self.ensure()
        timestamp = now or utc_now()

        def transition() -> AgentAction:
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                row = self._owned_action_row(database, action_id, account_id)
                row = self._expire_row_if_due(database, row, timestamp)
                current = str(row["status"])
                if current == destination:
                    return self._action(row)
                if current not in allowed_sources:
                    raise ActionStateError(f"cannot transition action from {current} to {destination}")
                fields = ["status = ?", "modified_at = ?"]
                values: list[Any] = [destination, _iso(timestamp)]
                if destination == "confirmed":
                    fields.append("confirmed_at = ?")
                    values.append(_iso(timestamp))
                elif destination == "executing":
                    fields.append("executing_at = ?")
                    values.append(_iso(timestamp))
                elif destination in {"succeeded", "failed", "cancelled"}:
                    fields.append("completed_at = ?")
                    values.append(_iso(timestamp))
                if destination in {"succeeded", "failed"}:
                    fields.extend(["result_summary_json = ?", "error = ?"])
                    values.extend([result_summary_json, error])
                values.append(action_id)
                database.execute(f"UPDATE webui_agent_actions SET {', '.join(fields)} WHERE action_id = ?", values)
                updated = database.execute(
                    "SELECT * FROM webui_agent_actions WHERE action_id = ?", (action_id,)
                ).fetchone()
                assert updated is not None
                return self._action(updated)

        return await asyncio.to_thread(transition)

    async def expire_due_actions(self, *, now: datetime | None = None) -> int:
        await self.ensure()
        timestamp = now or utc_now()

        def expire() -> int:
            with self._connect() as database:
                cursor = database.execute(
                    """
                    UPDATE webui_agent_actions
                    SET status = 'expired', modified_at = ?, completed_at = ?
                    WHERE status IN ('pending', 'confirmed') AND expires_at <= ?
                    """,
                    (_iso(timestamp), _iso(timestamp), _iso(timestamp)),
                )
                return cursor.rowcount

        return await asyncio.to_thread(expire)

    async def list_actions(
        self, *, account_id: str, limit: int = 100, now: datetime | None = None
    ) -> list[AgentAction]:
        await self.expire_due_actions(now=now)

        def read() -> list[AgentAction]:
            with self._connect() as database:
                rows = database.execute(
                    "SELECT * FROM webui_agent_actions WHERE account_id = ? ORDER BY created_at DESC LIMIT ?",
                    (account_id, max(1, min(limit, 500))),
                ).fetchall()
                return [self._action(row) for row in rows]

        return await asyncio.to_thread(read)

    async def cleanup_completed_actions(
        self,
        *,
        retention: timedelta = timedelta(days=30),
        now: datetime | None = None,
    ) -> int:
        """仅删除超过保留期限的终态操作元数据。"""

        await self.ensure()
        if retention < timedelta(0):
            raise ValueError("action retention must not be negative")
        cutoff = (now or utc_now()) - retention

        def delete() -> int:
            with self._connect() as database:
                cursor = database.execute(
                    """
                    DELETE FROM webui_agent_actions
                    WHERE status IN ('succeeded', 'failed', 'cancelled', 'expired')
                      AND completed_at IS NOT NULL AND completed_at <= ?
                    """,
                    (_iso(cutoff),),
                )
                return cursor.rowcount

        return await asyncio.to_thread(delete)

    @staticmethod
    def _owned_action_row(database: sqlite3.Connection, action_id: str, account_id: str) -> sqlite3.Row:
        row = database.execute("SELECT * FROM webui_agent_actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            raise ActionNotFoundError("action does not exist")
        if row["account_id"] != account_id:
            raise ActionOwnershipError("action belongs to another account")
        return row

    @staticmethod
    def _expire_row_if_due(database: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> sqlite3.Row:
        expires_at = _required_datetime(row["expires_at"], "webui_agent_actions.expires_at")
        if row["status"] in {"pending", "confirmed"} and expires_at <= now:
            database.execute(
                "UPDATE webui_agent_actions SET status = 'expired', modified_at = ?, "
                "completed_at = ? WHERE action_id = ?",
                (_iso(now), _iso(now), row["action_id"]),
            )
            updated = database.execute(
                "SELECT * FROM webui_agent_actions WHERE action_id = ?", (row["action_id"],)
            ).fetchone()
            assert updated is not None
            return updated
        return row

    @staticmethod
    def _safe_summary(summary: dict[str, Any] | None) -> str | None:
        if summary is None:
            return None
        safe, encoded = _canonicalize_arguments(summary)
        del safe
        return encoded

    @staticmethod
    def _action(row: sqlite3.Row, arguments: dict[str, Any] | None = None) -> AgentAction:
        return AgentAction(
            action_id=row["action_id"],
            account_id=row["account_id"],
            conversation_id=row["conversation_id"],
            tool_name=row["tool_name"],
            canonical_arguments=arguments or json.loads(row["canonical_arguments_json"]),
            required_permissions=json.loads(row["required_permissions_json"]),
            target=ActionTarget.model_validate_json(row["target_json"]) if row["target_json"] else None,
            risk_level=row["risk_level"],
            confirmation_mode=row["confirmation_mode"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            result_summary=json.loads(row["result_summary_json"]) if row["result_summary_json"] else None,
            error=row["error"],
            created_at=_required_datetime(row["created_at"], "webui_agent_actions.created_at"),
            modified_at=_required_datetime(row["modified_at"], "webui_agent_actions.modified_at"),
            expires_at=_required_datetime(row["expires_at"], "webui_agent_actions.expires_at"),
            confirmed_at=_datetime(row["confirmed_at"]),
            executing_at=_datetime(row["executing_at"]),
            completed_at=_datetime(row["completed_at"]),
        )

    async def create_artifact(self, artifact: AgentArtifact) -> AgentArtifact:
        await self.ensure()

        def create() -> AgentArtifact:
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                database.execute(
                    """
                    INSERT INTO webui_agent_artifacts(
                        artifact_id, owner_account_id, conversation_id, message_id, file_name, mime_type,
                        size_bytes, sha256, storage_path, created_at, expires_at, revoked_at, revoked_by_account_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id,
                        artifact.owner_account_id,
                        artifact.conversation_id,
                        artifact.message_id,
                        artifact.file_name,
                        artifact.mime_type,
                        artifact.size_bytes,
                        artifact.sha256,
                        artifact.storage_path,
                        _iso(artifact.created_at),
                        _iso(artifact.expires_at),
                        _iso(artifact.revoked_at) if artifact.revoked_at else None,
                        artifact.revoked_by_account_id,
                    ),
                )
                database.executemany(
                    "INSERT INTO webui_agent_artifact_permissions(artifact_id, permission_key) VALUES (?, ?)",
                    [(artifact.artifact_id, permission) for permission in sorted(set(artifact.required_permissions))],
                )
                database.executemany(
                    """
                    INSERT OR IGNORE INTO webui_agent_artifact_resources(
                        artifact_id, resource_type, resource_id, required_permission
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            artifact.artifact_id,
                            requirement.resource_type,
                            requirement.resource_id,
                            requirement.required_permission,
                        )
                        for requirement in artifact.resource_requirements
                    ],
                )
            return artifact

        return await asyncio.to_thread(create)

    async def get_artifact(self, artifact_id: str) -> AgentArtifact:
        await self.ensure()

        def read() -> AgentArtifact:
            with self._connect() as database:
                row = database.execute(
                    "SELECT * FROM webui_agent_artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                if row is None:
                    raise ArtifactNotFoundError("artifact does not exist")
                return self._artifact(database, row)

        return await asyncio.to_thread(read)

    async def revoke_artifact(
        self,
        artifact_id: str,
        *,
        account_id: str,
        allow_non_owner: bool = False,
        now: datetime | None = None,
    ) -> AgentArtifact:
        await self.ensure()
        timestamp = now or utc_now()

        def revoke() -> AgentArtifact:
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                row = database.execute(
                    "SELECT * FROM webui_agent_artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                if row is None:
                    raise ArtifactNotFoundError("artifact does not exist")
                if row["owner_account_id"] != account_id and not allow_non_owner:
                    raise ActionOwnershipError("only the artifact owner can revoke it")
                if row["revoked_at"] is None:
                    database.execute(
                        "UPDATE webui_agent_artifacts SET revoked_at = ?, revoked_by_account_id = ? "
                        "WHERE artifact_id = ?",
                        (_iso(timestamp), account_id, artifact_id),
                    )
                    row = database.execute(
                        "SELECT * FROM webui_agent_artifacts WHERE artifact_id = ?", (artifact_id,)
                    ).fetchone()
                    assert row is not None
                return self._artifact(database, row)

        return await asyncio.to_thread(revoke)

    async def require_available_artifact(self, artifact_id: str, *, now: datetime | None = None) -> AgentArtifact:
        artifact = await self.get_artifact(artifact_id)
        if not artifact.is_available(now or utc_now()):
            raise ArtifactUnavailableError("artifact is expired or revoked")
        return artifact

    async def record_artifact_download(
        self, artifact_id: str, *, account_id: str, now: datetime | None = None
    ) -> ArtifactDownloadRecord:
        downloaded_at = now or utc_now()
        record = ArtifactDownloadRecord(
            download_id=uuid4().hex,
            artifact_id=artifact_id,
            account_id=account_id,
            downloaded_at=downloaded_at,
        )

        def create() -> None:
            with self._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                row = database.execute(
                    "SELECT expires_at, revoked_at FROM webui_agent_artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                if row is None:
                    raise ArtifactNotFoundError("artifact does not exist")
                expires_at = _required_datetime(row["expires_at"], "webui_agent_artifacts.expires_at")
                if row["revoked_at"] is not None or expires_at <= downloaded_at:
                    raise ArtifactUnavailableError("artifact is expired or revoked")
                database.execute(
                    "INSERT INTO webui_agent_artifact_downloads VALUES (?, ?, ?, ?)",
                    (record.download_id, record.artifact_id, record.account_id, _iso(record.downloaded_at)),
                )

        await asyncio.to_thread(create)
        return record

    async def list_expired_artifacts(self, *, now: datetime | None = None) -> list[AgentArtifact]:
        await self.ensure()
        timestamp = now or utc_now()

        def read() -> list[AgentArtifact]:
            with self._connect() as database:
                rows = database.execute(
                    "SELECT * FROM webui_agent_artifacts WHERE expires_at <= ?", (_iso(timestamp),)
                ).fetchall()
                return [self._artifact(database, row) for row in rows]

        return await asyncio.to_thread(read)

    async def delete_artifact_metadata(self, artifact_id: str) -> None:
        await self.ensure()

        def delete() -> None:
            with self._connect() as database:
                database.execute("DELETE FROM webui_agent_artifacts WHERE artifact_id = ?", (artifact_id,))

        await asyncio.to_thread(delete)

    @staticmethod
    def _artifact(database: sqlite3.Connection, row: sqlite3.Row) -> AgentArtifact:
        permissions = [
            item[0]
            for item in database.execute(
                "SELECT permission_key FROM webui_agent_artifact_permissions "
                "WHERE artifact_id = ? ORDER BY permission_key",
                (row["artifact_id"],),
            )
        ]
        requirements = [
            ArtifactResourceRequirement(resource_type=item[0], resource_id=item[1], required_permission=item[2])
            for item in database.execute(
                """
                SELECT resource_type, resource_id, required_permission
                FROM webui_agent_artifact_resources WHERE artifact_id = ?
                ORDER BY resource_type, resource_id, required_permission
                """,
                (row["artifact_id"],),
            )
        ]
        return AgentArtifact(
            artifact_id=row["artifact_id"],
            owner_account_id=row["owner_account_id"],
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            file_name=row["file_name"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            storage_path=row["storage_path"],
            required_permissions=permissions,
            resource_requirements=requirements,
            created_at=_required_datetime(row["created_at"], "webui_agent_artifacts.created_at"),
            expires_at=_required_datetime(row["expires_at"], "webui_agent_artifacts.expires_at"),
            revoked_at=_datetime(row["revoked_at"]),
            revoked_by_account_id=row["revoked_by_account_id"],
        )
