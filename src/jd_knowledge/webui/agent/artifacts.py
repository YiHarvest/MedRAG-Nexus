"""Agent 临时制品的安全文件生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .models import AgentArtifact, ArtifactResourceRequirement
from .store import AgentStore, ArtifactUnavailableError, utc_now


class UnsafeArtifactPathError(RuntimeError):
    code = "unsafe_artifact_path"


class ArtifactService:
    DEFAULT_TTL = timedelta(hours=24)

    def __init__(self, root: Path, store: AgentStore):
        self.root = root
        self.store = store

    async def create(
        self,
        *,
        owner_account_id: str,
        file_name: str,
        mime_type: str,
        content: bytes,
        conversation_id: str | None = None,
        message_id: str | None = None,
        required_permissions: tuple[str, ...] | list[str] = (),
        resource_requirements: tuple[ArtifactResourceRequirement, ...] | list[ArtifactResourceRequirement] = (),
        ttl: timedelta = DEFAULT_TTL,
        now: datetime | None = None,
    ) -> AgentArtifact:
        if ttl <= timedelta(0):
            raise ValueError("artifact ttl must be positive")
        self._validate_file_name(file_name)
        if not mime_type or "\n" in mime_type or "\r" in mime_type:
            raise ValueError("invalid artifact mime type")
        timestamp = now or utc_now()
        artifact_id = uuid4().hex
        storage_path = f"{artifact_id}/content"
        artifact = AgentArtifact(
            artifact_id=artifact_id,
            owner_account_id=owner_account_id,
            conversation_id=conversation_id,
            message_id=message_id,
            file_name=file_name,
            mime_type=mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            storage_path=storage_path,
            required_permissions=sorted(set(required_permissions)),
            resource_requirements=list(resource_requirements),
            created_at=timestamp,
            expires_at=timestamp + ttl,
        )
        path = self._safe_path(artifact)
        await asyncio.to_thread(self._atomic_write, path, content)
        try:
            return await self.store.create_artifact(artifact)
        except Exception:
            await asyncio.to_thread(shutil.rmtree, path.parent, True)
            raise

    async def resolve_download(self, artifact_id: str, *, now: datetime | None = None) -> tuple[AgentArtifact, Path]:
        artifact = await self.store.require_available_artifact(artifact_id, now=now)
        path = self._safe_path(artifact)
        if not await asyncio.to_thread(path.is_file):
            raise ArtifactUnavailableError("artifact file is unavailable")
        return artifact, path

    async def revoke(
        self,
        artifact_id: str,
        *,
        account_id: str,
        allow_non_owner: bool = False,
        now: datetime | None = None,
    ) -> AgentArtifact:
        artifact = await self.store.revoke_artifact(
            artifact_id, account_id=account_id, allow_non_owner=allow_non_owner, now=now
        )
        path = self._safe_path(artifact)
        await asyncio.to_thread(shutil.rmtree, path.parent, True)
        return artifact

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        artifacts = await self.store.list_expired_artifacts(now=now)
        cleaned = 0
        for artifact in artifacts:
            path = self._safe_path(artifact)
            await asyncio.to_thread(shutil.rmtree, path.parent, True)
            await self.store.delete_artifact_metadata(artifact.artifact_id)
            cleaned += 1
        return cleaned

    def _safe_path(self, artifact: AgentArtifact) -> Path:
        root = self.root.resolve()
        relative = Path(artifact.storage_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise UnsafeArtifactPathError("artifact storage path escapes the configured root")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise UnsafeArtifactPathError("artifact storage path escapes the configured root") from exc
        expected = root / artifact.artifact_id / "content"
        if candidate != expected:
            raise UnsafeArtifactPathError("artifact storage path does not match its identifier")
        return candidate

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=False)
        temporary = path.parent / f".{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_file_name(file_name: str) -> None:
        if not file_name or len(file_name) > 255 or "\x00" in file_name or "\r" in file_name or "\n" in file_name:
            raise ValueError("invalid artifact file name")
        if Path(file_name).name != file_name or file_name in {".", ".."}:
            raise ValueError("artifact file name must not contain a path")
