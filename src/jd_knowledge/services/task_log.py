"""任务、检索与 API 访问日志存储 - 按天归档，定期清理过期日志。

目录结构：
    data/log/tasks/<task_id>.log              # 任务日志
    data/log/retrieval/<YYYY-MM-DD>.log       # 检索日志（按天归档）
    data/log/api/<YYYY-MM-DD>.log             # API 访问日志（按天归档）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from jd_knowledge.core.models import local_now

logger = logging.getLogger(__name__)
terminal_logger = logging.getLogger("uvicorn.error")

LogLevel = str


def _single_line(value: object) -> str:
    """压平换行并移除终端控制字符，防止日志伪造和 ANSI 转义注入。"""
    return "".join(character if character.isprintable() else " " for character in str(value))


class TaskLogStore:
    """按任务写入日志文件，并清理超过保留期的旧日志。

    不同任务写入不同文件；同一任务的写入在单个事件循环内串行执行，
    因此不存在并发写同一文件的冲突。
    """

    def __init__(self, root: Path):
        self.log_root = root / "log"
        self.tasks_dir = self.log_root / "tasks"
        self.retrieval_dir = self.log_root / "retrieval"
        self.api_dir = self.log_root / "api"

    async def ensure(self) -> None:
        await asyncio.to_thread(self.log_root.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self.tasks_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self.retrieval_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(self.api_dir.mkdir, parents=True, exist_ok=True)

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.log"

    @staticmethod
    def _terminal(channel: str, level: str, message: str, context: dict[str, object]) -> None:
        """将结构化业务日志同步到 Uvicorn 终端，避免打印多行正文或控制字符。"""
        log_level = getattr(logging, level.upper(), logging.INFO)
        safe_message = _single_line(message)
        extras = " ".join(f"{key}={_single_line(value)}" for key, value in context.items() if value is not None)
        terminal_logger.log(log_level, "[%s] %s%s", channel, safe_message, f" | {extras}" if extras else "")

    async def _append(
        self,
        path: Path,
        level: str,
        message: str,
        context: dict[str, object],
        *,
        channel: str,
    ) -> None:
        """同步输出终端并异步追加日志文件；文件写入失败不影响业务处理。"""
        self._terminal(channel, level, message, context)

        def append() -> None:
            timestamp = local_now().isoformat(timespec="seconds")
            safe_message = _single_line(message)
            extras = " ".join(f"{key}={_single_line(value)}" for key, value in context.items() if value is not None)
            line = f"{timestamp} [{level}] {extras} {safe_message}\n"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

        try:
            await asyncio.to_thread(append)
        except OSError:
            logger.warning("failed to append log file %s", path, exc_info=True)

    async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
        """写入任务日志（每个任务一个文件）。"""
        await self._append(
            self.task_path(task_id),
            level,
            message,
            {"task": task_id, **context},
            channel="TASK",
        )

    async def write_retrieval(self, level: str, message: str, **context: object) -> None:
        """写入检索日志（按天归档到 retrieval/<YYYY-MM-DD>.log）。"""
        today = local_now().date().isoformat()
        await self._append(
            self.retrieval_dir / f"{today}.log",
            level,
            message,
            context,
            channel="RETRIEVAL",
        )

    async def write_api(self, level: str, message: str, **context: object) -> None:
        """写入 API 访问日志（按天归档到 api/<YYYY-MM-DD>.log）。"""
        today = local_now().date().isoformat()
        await self._append(
            self.api_dir / f"{today}.log",
            level,
            message,
            context,
            channel="API",
        )

    async def cleanup(self, older_than_days: int = 7) -> int:
        """删除 mtime 早于保留期的任务与检索日志文件，返回删除数量。"""
        if not await asyncio.to_thread(self.log_root.is_dir):
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400

        def scan() -> int:
            deleted = 0
            for path in self.log_root.rglob("*.log"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        deleted += 1
                except OSError:
                    continue
            return deleted

        deleted = await asyncio.to_thread(scan)
        if deleted:
            logger.info("cleaned %d stale logs older than %d days", deleted, older_than_days)
        return deleted
