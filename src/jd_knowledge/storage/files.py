"""管理知识制品、暂存目录与回收快照的文件存储。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from jd_knowledge.core.ids import content_hash
from jd_knowledge.core.models import ResourceRecord, local_now


class ArtifactStore:
    def __init__(self, data_root: Path):
        self.root = data_root
        self.workspaces_root = self.root / "workspaces"
        self.staging_root = self.root / "v3_staging"
        self.recycle_root = self.root / "v3_recycle"

    async def ensure(self) -> None:
        for path in (self.workspaces_root, self.staging_root, self.recycle_root):
            await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        probe = self.root / ".write-probe"
        await asyncio.to_thread(probe.write_text, "ok", encoding="utf-8")
        await asyncio.to_thread(probe.unlink, missing_ok=True)

    @staticmethod
    def user_key(user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def workspace_dir(self, user_id: str, workspace_id: str) -> Path:
        return self.workspaces_root / self.user_key(user_id) / workspace_id

    def staging_dir(self, task_id: str) -> Path:
        return self.staging_root / task_id

    def recycle_dir(self, task_id: str) -> Path:
        return self.recycle_root / task_id

    async def stage_bytes(self, task_id: str, file_name: str, content: bytes) -> Path:
        directory = self.staging_dir(task_id) / "input"
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=False)
        target = directory / file_name
        temporary = directory / f".{file_name}.partial"
        await asyncio.to_thread(temporary.write_bytes, content)
        await asyncio.to_thread(os.replace, temporary, target)
        return target

    async def stage_text(self, task_id: str, content: str) -> Path:
        return await self.stage_bytes(task_id, "content.txt", content.encode("utf-8"))

    def file_target(self, user_id: str, workspace_id: str, file_id: str) -> Path:
        return self.workspace_dir(user_id, workspace_id) / "files" / file_id

    async def prepare_file(
        self,
        *,
        task_id: str,
        user_id: str,
        workspace_id: str,
        file_id: str,
        file_name: str,
        source_path: Path,
        markdown: str,
    ) -> tuple[Path, Path]:
        prepared = self.staging_dir(task_id) / "publish" / file_id
        await asyncio.to_thread((prepared / "raw").mkdir, parents=True, exist_ok=False)
        await asyncio.to_thread(shutil.copy2, source_path, prepared / "raw" / file_name)
        await asyncio.to_thread((prepared / "document.md").write_text, markdown, encoding="utf-8")
        return prepared, self.file_target(user_id, workspace_id, file_id)

    @staticmethod
    def publish_directory(prepared: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        os.replace(prepared, target)

    def strings_target(self, user_id: str, workspace_id: str) -> Path:
        return self.workspace_dir(user_id, workspace_id) / "strings.jsonl"

    async def prepare_string_record(
        self,
        *,
        task_id: str,
        user_id: str,
        workspace_id: str,
        record: dict[str, Any],
    ) -> tuple[Path, Path]:
        target = self.strings_target(user_id, workspace_id)
        prepared = self.staging_dir(task_id) / "publish" / "strings.jsonl"
        await asyncio.to_thread(prepared.parent.mkdir, parents=True, exist_ok=True)

        def prepare() -> None:
            existing = target.read_bytes() if target.is_file() else b""
            line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            prepared.write_bytes(existing + line)

        await asyncio.to_thread(prepare)
        return prepared, target

    @staticmethod
    def publish_file(prepared: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(prepared, target)

    async def read_string_contents(self, user_id: str, workspace_id: str) -> dict[str, str]:
        path = self.strings_target(user_id, workspace_id)

        def read() -> dict[str, str]:
            if not path.is_file():
                return {}
            values: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                digest = record.get("content_hash")
                content = record.get("content")
                if isinstance(digest, str) and isinstance(content, str):
                    values[digest] = content
            return values

        return await asyncio.to_thread(read)

    async def read_string_record(
        self,
        user_id: str,
        workspace_id: str,
        content_hash: str,
    ) -> dict[str, Any] | None:
        path = self.strings_target(user_id, workspace_id)

        def read() -> dict[str, Any] | None:
            if not path.is_file():
                return None
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("content_hash") == content_hash:
                    return record
            return None

        return await asyncio.to_thread(read)

    async def remove_string_record(self, user_id: str, workspace_id: str, content_hash: str) -> None:
        path = self.strings_target(user_id, workspace_id)

        def rewrite() -> None:
            if not path.is_file():
                return
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            records = [record for record in records if record.get("content_hash") != content_hash]
            temporary = path.with_suffix(".jsonl.partial")
            temporary.write_text(
                "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records),
                encoding="utf-8",
            )
            os.replace(temporary, path)

        await asyncio.to_thread(rewrite)

    async def restore_string_record(
        self,
        user_id: str,
        workspace_id: str,
        record: dict[str, Any],
    ) -> None:
        path = self.strings_target(user_id, workspace_id)
        digest = record.get("content_hash")
        if not isinstance(digest, str):
            raise ValueError("string record is missing content_hash")

        def restore() -> None:
            existing: list[dict[str, Any]] = []
            if path.is_file():
                existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if any(value.get("content_hash") == digest for value in existing):
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".jsonl.partial")
            temporary.write_text(
                "".join(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                    for value in [*existing, record]
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)

        await asyncio.to_thread(restore)

    async def read_markdown(self, artifact_path: str) -> str:
        return await asyncio.to_thread((Path(artifact_path) / "document.md").read_text, encoding="utf-8")

    def raw_file_path(self, resource: ResourceRecord) -> Path:
        """解析持久化原始文件，绝不信任调用方提供的路径。"""

        if resource.source_type != "file" or not resource.file_id or not resource.file_name:
            raise FileNotFoundError("resource does not contain a raw file")
        expected_root = self.file_target(
            resource.user_id,
            resource.workspace_id,
            resource.file_id,
        ).resolve()
        artifact_root = Path(resource.artifact_path).resolve()
        if artifact_root != expected_root:
            raise FileNotFoundError("resource artifact path is outside its expected location")
        target = (artifact_root / "raw" / resource.file_name).resolve()
        if target.parent != (artifact_root / "raw").resolve() or not target.is_file():
            raise FileNotFoundError("raw file is unavailable")
        return target

    async def resource_is_complete(self, resource: ResourceRecord) -> bool:
        """验证 SQLite 元数据指向已经完整发布的持久化制品。"""

        if resource.source_type == "str":
            contents = await self.read_string_contents(resource.user_id, resource.workspace_id)
            value = contents.get(resource.content_hash)
            return value is not None and len(value.encode("utf-8")) == resource.size_bytes

        def check_file() -> bool:
            if not resource.file_name or not resource.markdown_hash:
                return False
            root = Path(resource.artifact_path)
            raw = root / "raw" / resource.file_name
            markdown = root / "document.md"
            if not root.is_dir() or not raw.is_file() or not markdown.is_file():
                return False
            if raw.stat().st_size != resource.size_bytes:
                return False
            return content_hash(markdown.read_bytes()) == resource.markdown_hash

        return await asyncio.to_thread(check_file)

    async def move_to_recycle(self, task_id: str, artifact_path: str) -> Path:
        source = Path(artifact_path)
        target = self.recycle_dir(task_id) / source.name

        def move() -> None:
            if not source.is_dir():
                raise FileNotFoundError(source)
            target.parent.mkdir(parents=True, exist_ok=False)
            os.replace(source, target)

        await asyncio.to_thread(move)
        return target

    async def move_workspace_to_recycle(
        self,
        operation_id: str,
        user_id: str,
        workspace_id: str,
    ) -> Path | None:
        """原子隐藏知识库目录；空知识库可能没有对应目录。"""

        source = self.workspace_dir(user_id, workspace_id)
        target = self.recycle_dir(operation_id) / "workspace"

        def move() -> Path | None:
            if not source.exists():
                return None
            if not source.is_dir():
                raise NotADirectoryError(source)
            target.parent.mkdir(parents=True, exist_ok=False)
            os.replace(source, target)
            return target

        return await asyncio.to_thread(move)

    async def write_recycle_snapshot(self, task_id: str, snapshot: dict[str, Any]) -> None:
        target = self.recycle_dir(task_id) / ".index-snapshot.json"
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        await asyncio.to_thread(target.write_text, payload, encoding="utf-8")

    async def read_recycle_snapshot(self, task_id: str) -> dict[str, Any] | None:
        target = self.recycle_dir(task_id) / ".index-snapshot.json"
        if not target.is_file():
            return None
        return json.loads(await asyncio.to_thread(target.read_text, encoding="utf-8"))

    async def restore_from_recycle(self, recycled: Path, artifact_path: str) -> None:
        target = Path(artifact_path)

        def restore() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(target)
            os.replace(recycled, target)

        await asyncio.to_thread(restore)

    async def cleanup_recycle(self, task_id: str) -> None:
        target = self.recycle_dir(task_id)
        if await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(shutil.rmtree, target)

    async def delete_user_artifacts(self, user_id: str) -> None:
        """删除知识用户在 Workspace 根目录下的剩余制品目录。"""

        target = self.workspaces_root / self.user_key(user_id)
        if await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(shutil.rmtree, target)

    async def cleanup_staging(self, task_id: str) -> None:
        target = self.staging_dir(task_id)
        if await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(shutil.rmtree, target)

    async def delete_artifact(self, artifact_path: str) -> None:
        target = Path(artifact_path)
        if await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(shutil.rmtree, target)

    async def write_delete_backup(self, task_id: str, payload: dict[str, Any]) -> Path:
        path = self.staging_dir(task_id) / "delete-backup.json"
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            path.write_text,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return path

    async def read_delete_backup(self, task_id: str) -> dict[str, Any] | None:
        path = self.staging_dir(task_id) / "delete-backup.json"
        if not await asyncio.to_thread(path.is_file):
            return None
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return json.loads(text)

    async def cleanup_stale_staging(self, older_than_hours: int = 24) -> int:
        if not self.staging_root.is_dir():
            return 0
        cutoff = local_now().timestamp() - older_than_hours * 3600
        deleted = 0
        for path in self.staging_root.iterdir():
            if path.is_dir() and path.stat().st_mtime < cutoff:
                await asyncio.to_thread(shutil.rmtree, path)
                deleted += 1
        return deleted

    async def delete_legacy_layout(self) -> None:
        for name in ("documents", "staging"):
            path = self.root / name
            if await asyncio.to_thread(path.is_dir):
                await asyncio.to_thread(shutil.rmtree, path)
