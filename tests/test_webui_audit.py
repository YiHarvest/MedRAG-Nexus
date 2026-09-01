"""验证 WebUI 独立存储配置与文件审计归档。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from medrag_nexus.core.config import Settings
from medrag_nexus.identity.audit import (
    AuditLogExporter,
    reset_audit_request_id,
    set_audit_request_id,
)
from medrag_nexus.identity.permissions import build_default_registry
from medrag_nexus.identity.store import AccountStore


def test_backend_runtime_settings_isolate_all_storage_names(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        openai_embedding_url="http://embedding.test/v1/embeddings",
        data_root=tmp_path / "public",
        sqlite_path=tmp_path / "public.sqlite3",
        webui_data_root=tmp_path / "webui",
        webui_sqlite_path=tmp_path / "webui.sqlite3",
        elasticsearch_url="http://es.test:9200",
        milvus_host="milvus.test",
        milvus_collection="public_chunks",
        webui_milvus_collection="webui_chunks",
        elasticsearch_workspace_index="public_workspaces",
        elasticsearch_document_index="public_resources",
        elasticsearch_chunk_index="public_chunks",
        webui_elasticsearch_workspace_index="webui_workspaces",
        webui_elasticsearch_document_index="webui_resources",
        webui_elasticsearch_chunk_index="webui_chunks",
    )

    backend = settings.backend_runtime_settings()

    assert backend.sqlite_path == settings.webui_sqlite_path
    assert backend.data_root == settings.webui_data_root
    assert backend.elasticsearch_url == settings.elasticsearch_url
    assert backend.elasticsearch_chunk_index != settings.elasticsearch_chunk_index
    assert backend.milvus_host == settings.milvus_host
    assert backend.milvus_collection != settings.milvus_collection
    assert backend.redis_queue_name != settings.redis_queue_name


async def test_audit_events_export_with_actor_and_request_id(tmp_path) -> None:
    database_path = tmp_path / "webui.sqlite3"
    log_dir = tmp_path / "audit"
    store = AccountStore(database_path, build_default_registry())
    await store.ensure()
    account = await store.register_account(
        login_name="audit-user",
        display_name="审计用户",
        password_hash="not-a-real-password-hash",
    )
    request_context = set_audit_request_id("request-123")
    try:
        await store.record_audit(
            actor_account_id=account.account_id,
            action="webui.resource.file.add",
            resource_type="file",
            resource_id="file-1",
            after={"file_name": "report.pdf", "size_bytes": 42},
        )
    finally:
        reset_audit_request_id(request_context)

    exporter = AuditLogExporter(database_path, log_dir, retention_months=3)
    await exporter.ensure()
    assert await exporter.export_pending() >= 1
    assert await exporter.export_pending() == 0

    events = [
        json.loads(line)
        for path in log_dir.glob("webui-audit-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    uploaded = next(event for event in events if event["action"] == "webui.resource.file.add")
    assert uploaded["request_id"] == "request-123"
    assert uploaded["actor"]["account_id"] == account.account_id
    assert uploaded["actor"]["login_name"] == "audit-user"
    assert uploaded["after"] == {"file_name": "report.pdf", "size_bytes": 42}


async def test_audit_cleanup_removes_only_expired_daily_logs(tmp_path) -> None:
    database_path = tmp_path / "webui.sqlite3"
    store = AccountStore(database_path, build_default_registry())
    await store.ensure()
    log_dir = tmp_path / "audit"
    exporter = AuditLogExporter(database_path, log_dir, retention_months=3)
    await exporter.ensure()
    await store.record_audit(
        actor_account_id=None,
        action="webui.expired.test",
        resource_type="test",
        resource_id="expired",
    )
    with sqlite3.connect(database_path) as database:
        database.execute(
            "UPDATE webui_audit_events SET created_at = ? WHERE action = ?",
            ("2020-01-01T00:00:00+00:00", "webui.expired.test"),
        )
    expired = log_dir / f"webui-audit-{(date.today() - timedelta(days=370)).isoformat()}.jsonl"
    recent = log_dir / f"webui-audit-{date.today().isoformat()}.jsonl"
    expired.write_text("{}\n", encoding="utf-8")
    recent.write_text("{}\n", encoding="utf-8")

    assert await exporter.cleanup() == 1
    assert not expired.exists()
    assert recent.exists()
    events, _ = await store.list_audit_events(limit=100)
    assert all(event.action != "webui.expired.test" for event in events)
