"""将 WebUI 数据库审计事件可靠导出为按日归档的 JSONL 文件。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from contextvars import ContextVar, Token
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_REQUEST_ID: ContextVar[str | None] = ContextVar("webui_audit_request_id", default=None)


def set_audit_request_id(request_id: str) -> Token[str | None]:
    return _REQUEST_ID.set(request_id)


def reset_audit_request_id(token: Token[str | None]) -> None:
    _REQUEST_ID.reset(token)


def current_audit_request_id() -> str | None:
    return _REQUEST_ID.get()


def _retention_cutoff(today: date, months: int) -> date:
    """返回按自然月计算的保留边界，并处理短月份。"""

    year = today.year
    month = today.month - months
    while month <= 0:
        year -= 1
        month += 12
    for day in range(today.day, 0, -1):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 1)


class WebUiAuditLogExporter:
    """把已提交的 SQLite 审计记录导出到仅追加的结构化日志文件。

    SQLite 中的导出标记用于多进程互斥和断点续传。文件写入成功后才记录
    标记，因此异常退出最多产生可按 ``event_id`` 去重的重复行，不会漏事件。
    """

    def __init__(self, database_path: Path, log_dir: Path, retention_months: int):
        self.database_path = database_path
        self.log_dir = log_dir
        self.retention_months = retention_months
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        await asyncio.to_thread(self._ensure_sync)
        await self.cleanup()

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout = 30000")
        return database

    def _ensure_sync(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS webui_audit_file_exports ("
                "event_id TEXT PRIMARY KEY, exported_at TEXT NOT NULL)"
            )
        try:
            self.log_dir.chmod(0o750)
        except OSError:
            logger.warning("无法修改 WebUI 审计日志目录权限", exc_info=True)

    async def export_pending(self, *, limit: int = 1000) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._export_pending_sync, limit)

    def _export_pending_sync(self, limit: int) -> int:
        exported = 0
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                rows = database.execute(
                    "SELECT e.event_id, e.actor_account_id, a.login_name, a.display_name, "
                    "a.permission_level, e.action, e.resource_type, e.resource_id, "
                    "e.before_json, e.after_json, e.request_id, e.created_at "
                    "FROM webui_audit_events e "
                    "LEFT JOIN webui_accounts a ON a.account_id = e.actor_account_id "
                    "LEFT JOIN webui_audit_file_exports x ON x.event_id = e.event_id "
                    "WHERE x.event_id IS NULL ORDER BY e.created_at, e.event_id LIMIT ?",
                    (limit,),
                ).fetchall()
                grouped: dict[date, list[tuple[str, str]]] = {}
                for row in rows:
                    created_at = datetime.fromisoformat(str(row["created_at"]))
                    payload: dict[str, Any] = {
                        "event_id": str(row["event_id"]),
                        "request_id": row["request_id"],
                        "created_at": created_at.astimezone(timezone.utc).isoformat(),
                        "actor": {
                            "account_id": row["actor_account_id"],
                            "login_name": row["login_name"],
                            "display_name": row["display_name"],
                            "permission_level": row["permission_level"],
                        },
                        "action": str(row["action"]),
                        "resource": {
                            "type": str(row["resource_type"]),
                            "id": str(row["resource_id"]),
                        },
                        "before": json.loads(row["before_json"]) if row["before_json"] else None,
                        "after": json.loads(row["after_json"]) if row["after_json"] else None,
                    }
                    grouped.setdefault(created_at.date(), []).append(
                        (
                            str(row["event_id"]),
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        )
                    )
                exported_at = datetime.now(timezone.utc).isoformat()
                for event_date, entries in grouped.items():
                    target = self.log_dir / f"webui-audit-{event_date.isoformat()}.jsonl"
                    with target.open("a", encoding="utf-8") as handle:
                        for _, line in entries:
                            handle.write(f"{line}\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    try:
                        target.chmod(0o640)
                    except OSError:
                        logger.warning("无法修改 WebUI 审计日志文件权限", extra={"path": str(target)})
                    database.executemany(
                        "INSERT OR IGNORE INTO webui_audit_file_exports(event_id, exported_at) VALUES (?, ?)",
                        [(event_id, exported_at) for event_id, _ in entries],
                    )
                    exported += len(entries)
                database.commit()
            except BaseException:
                database.rollback()
                raise
        return exported

    async def cleanup(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._cleanup_sync)

    def _cleanup_sync(self) -> int:
        cutoff = _retention_cutoff(datetime.now(timezone.utc).date(), self.retention_months)
        removed = 0
        for path in self.log_dir.glob("webui-audit-*.jsonl"):
            try:
                log_date = date.fromisoformat(path.stem.removeprefix("webui-audit-"))
            except ValueError:
                continue
            if log_date < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        cutoff_timestamp = datetime.combine(cutoff, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                database.execute(
                    "DELETE FROM webui_audit_file_exports WHERE event_id IN ("
                    "SELECT event_id FROM webui_audit_events WHERE created_at < ?)",
                    (cutoff_timestamp,),
                )
                database.execute(
                    "DELETE FROM webui_audit_events WHERE created_at < ?",
                    (cutoff_timestamp,),
                )
                database.commit()
            except BaseException:
                database.rollback()
                raise
        return removed
