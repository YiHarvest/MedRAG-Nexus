"""Tests for temporary Agent artifacts and in-memory Word export."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from docx import Document

from medrag_nexus.backend.agent import (
    ActionOwnershipError,
    AgentArtifactResponse,
    AgentStore,
    AnswerExportContent,
    AnswerSource,
    ArtifactResourceRequirement,
    ArtifactService,
    ArtifactUnavailableError,
    UnsafeArtifactPathError,
    export_answer_to_word,
)


async def test_word_artifact_has_24h_ttl_and_all_access_requirements(tmp_path) -> None:
    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    database_path = tmp_path / "webui.sqlite3"
    store = AgentStore(database_path)
    service = ArtifactService(tmp_path / "agent-artifacts", store)
    artifact = await export_answer_to_word(
        service,
        owner_account_id="account-a",
        conversation_id="conversation-1",
        message_id="message-1",
        content=AnswerExportContent(
            question="问题内容（临时）",
            answer="回答内容（临时）",
            generated_by="测试账号",
            sources=[AnswerSource(title="知识库文件", reference="file-a", excerpt="引用片段")],
        ),
        required_permissions=["webui.audit.read", "webui.account.read", "webui.audit.read"],
        resource_requirements=[
            ArtifactResourceRequirement(
                resource_type="workspace",
                resource_id="workspace-a",
                required_permission="webui.workspace.read",
            )
        ],
        now=now,
    )

    assert artifact.expires_at == now + timedelta(hours=24)
    assert artifact.required_permissions == ["webui.account.read", "webui.audit.read"]
    assert len(artifact.resource_requirements) == 1
    client_payload = AgentArtifactResponse.from_record(artifact).model_dump(mode="json")
    assert "storage_path" not in client_payload
    loaded, path = await service.resolve_download(artifact.artifact_id, now=now)
    assert loaded.sha256 == artifact.sha256
    document = Document(path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "问题内容（临时）" in text
    assert "回答内容（临时）" in text
    assert "引用片段" in text

    with sqlite3.connect(database_path) as database:
        database_text = "\n".join(
            str(value)
            for table in ("webui_agent_artifacts", "webui_agent_artifact_permissions", "webui_agent_artifact_resources")
            for row in database.execute(f"SELECT * FROM {table}")
            for value in row
        )
    assert "问题内容（临时）" not in database_text
    assert "回答内容（临时）" not in database_text

    download = await store.record_artifact_download(artifact.artifact_id, account_id="account-b", now=now)
    assert download.account_id == "account-b"


async def test_artifact_revoke_is_owner_bound_and_idempotent(tmp_path) -> None:
    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    store = AgentStore(tmp_path / "webui.sqlite3")
    service = ArtifactService(tmp_path / "agent-artifacts", store)
    artifact = await service.create(
        owner_account_id="account-a",
        file_name="answer.docx",
        mime_type="application/octet-stream",
        content=b"content",
        now=now,
    )
    with pytest.raises(ActionOwnershipError):
        await service.revoke(artifact.artifact_id, account_id="account-b", now=now)

    revoked = await service.revoke(artifact.artifact_id, account_id="account-a", now=now)
    assert revoked.revoked_at == now
    repeated = await service.revoke(artifact.artifact_id, account_id="account-a", now=now + timedelta(seconds=1))
    assert repeated.revoked_at == now
    with pytest.raises(ArtifactUnavailableError):
        await service.resolve_download(artifact.artifact_id, now=now)


async def test_expired_artifacts_are_safely_removed(tmp_path) -> None:
    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)
    database_path = tmp_path / "webui.sqlite3"
    store = AgentStore(database_path)
    service = ArtifactService(tmp_path / "agent-artifacts", store)
    artifact = await service.create(
        owner_account_id="account-a",
        file_name="answer.docx",
        mime_type="application/octet-stream",
        content=b"content",
        ttl=timedelta(seconds=1),
        now=now,
    )
    _, path = await service.resolve_download(artifact.artifact_id, now=now)
    assert path.exists()
    assert await service.cleanup_expired(now=now + timedelta(seconds=2)) == 1
    assert not path.exists()
    with pytest.raises(Exception, match="artifact does not exist"):
        await store.get_artifact(artifact.artifact_id)


async def test_tampered_storage_path_cannot_escape_root(tmp_path) -> None:
    store = AgentStore(tmp_path / "webui.sqlite3")
    service = ArtifactService(tmp_path / "agent-artifacts", store)
    artifact = await service.create(
        owner_account_id="account-a",
        file_name="answer.docx",
        mime_type="application/octet-stream",
        content=b"content",
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("do not expose", encoding="utf-8")
    with sqlite3.connect(tmp_path / "webui.sqlite3") as database:
        database.execute(
            "UPDATE webui_agent_artifacts SET storage_path = ? WHERE artifact_id = ?",
            ("../../outside.txt", artifact.artifact_id),
        )
    with pytest.raises(UnsafeArtifactPathError):
        await service.resolve_download(artifact.artifact_id)
    assert outside.read_text(encoding="utf-8") == "do not expose"
