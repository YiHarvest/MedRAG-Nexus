"""验证字符串知识删除会一致更新制品、索引与 SQLite 统计。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from medrag_nexus.core.ids import chunk_id, content_hash, new_id, new_task_id
from medrag_nexus.core.models import (
    ChunkRecord,
    DeleteStringRequest,
    ResourceRecord,
    TaskRecord,
    TaskStatus,
    WorkspaceRecord,
    local_now,
)
from medrag_nexus.services.files import FileService
from medrag_nexus.services.processing import _compensate_delete, process_delete
from medrag_nexus.storage.files import ArtifactStore
from medrag_nexus.storage.sqlite import SQLiteStore


class FakeTasks:
    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []

    @asynccontextmanager
    async def workspace_lock(self, user_id: str, workspace_id: str):
        yield

    async def release_content(
        self,
        user_id: str,
        workspace_id: str,
        source_type: str,
        digest: str,
        task_id: str,
    ) -> None:
        self.released.append((source_type, digest))


class FakeTaskLog:
    async def write_task(self, task_id: str, level: str, message: str, **context: object) -> None:
        return None


async def test_submit_delete_string_reserves_hash_and_enqueues_task() -> None:
    digest = "sha256:" + "a" * 32
    workspace = WorkspaceRecord(
        workspace_id="workspace_11111111-1111-5111-8111-111111111111",
        user_id="user-001",
        workspace_name="Knowledge",
    )
    resource = SimpleNamespace(content_hash=digest)

    class Metadata:
        def __init__(self) -> None:
            self.created: list[TaskRecord] = []

        async def workspace_repair_reason(self, workspace_id: str):
            return None

        async def get_workspace(self, workspace_id: str):
            return workspace

        async def get_string(self, workspace_id: str, content_hash: str):
            return resource

        async def create_task(self, task: TaskRecord) -> None:
            self.created.append(task)

    class Tasks:
        def __init__(self) -> None:
            self.reserved = []
            self.enqueued = []

        async def health(self) -> None:
            return None

        @asynccontextmanager
        async def workspace_lock(self, user_id: str, workspace_id: str, *, wait_seconds=None):
            yield

        async def reserve_content(self, user_id, workspace_id, source_type, content_hash, task_id):
            self.reserved.append((source_type, content_hash))
            return None

        async def release_content(self, user_id, workspace_id, source_type, content_hash, task_id):
            raise AssertionError("successful submission must retain its reservation for the worker")

        async def enqueue(self, task_id: str) -> None:
            self.enqueued.append(task_id)

    tasks = Tasks()
    metadata = Metadata()
    runtime = SimpleNamespace(metadata=metadata, tasks=tasks, task_log=FakeTaskLog())

    response = await FileService(runtime).submit_delete_string(
        DeleteStringRequest(user_id=workspace.user_id, workspace_id=workspace.workspace_id, content_hash=digest)
    )

    assert tasks.reserved == [("str", digest)]
    assert tasks.enqueued == [response.task_id]
    assert metadata.created[0].operation == "delete_string"
    assert metadata.created[0].payload == {
        "content_hash": digest,
        "allow_missing": False,
        "callback_url": None,
    }


class FakeElasticsearch:
    def __init__(self, chunk: ChunkRecord) -> None:
        self.chunk = chunk
        self.deleted = []
        self.workspace_updates = []

    async def get_chunks(self, workspace_id: str, document_id):
        return [self.chunk]

    async def delete_resource(self, workspace_id: str, document_id) -> None:
        self.deleted.append((workspace_id, document_id))

    async def mirror_workspace(self, workspace) -> None:
        self.workspace_updates.append(workspace)

    async def index_resource(self, resource, chunks) -> None:  # pragma: no cover - compensation only
        raise AssertionError("successful deletion must not compensate Elasticsearch")


class FakeMilvus:
    def __init__(self, chunk: ChunkRecord) -> None:
        self.chunk = chunk
        self.deleted = []

    async def get_resource_chunks(self, workspace_id: str, document_id):
        return [self.chunk], [[0.1, 0.2]]

    async def delete_resource(self, workspace_id: str, document_id) -> None:
        self.deleted.append((workspace_id, document_id))

    async def upsert_chunks(self, chunks, vectors) -> None:  # pragma: no cover - compensation only
        raise AssertionError("successful deletion must not compensate Milvus")


async def test_delete_string_removes_all_backends_and_updates_stats(tmp_path) -> None:
    metadata = SQLiteStore(tmp_path / "metadata.sqlite3")
    artifacts = ArtifactStore(tmp_path)
    await metadata.ensure()
    await artifacts.ensure()
    now = local_now()
    user_id = "user-001"
    workspace_id = "workspace_11111111-1111-5111-8111-111111111111"
    workspace_name = "Knowledge"
    target_digest = "sha256:" + "a" * 32
    sibling_digest = "sha256:" + "b" * 32
    target = artifacts.strings_target(user_id, workspace_id)
    target.parent.mkdir(parents=True)
    target_record = {
        "content": "remove",
        "content_hash": target_digest,
        "size_bytes": 6,
        "created_at": now.isoformat(),
        "modified_at": now.isoformat(),
    }
    sibling_record = {
        "content": "keep",
        "content_hash": sibling_digest,
        "size_bytes": 4,
        "created_at": now.isoformat(),
        "modified_at": now.isoformat(),
    }
    target.write_text(
        "".join(json.dumps(value, separators=(",", ":")) + "\n" for value in [target_record, sibling_record]),
        encoding="utf-8",
    )

    target_resource = ResourceRecord(
        document_id=new_id(),
        workspace_id=workspace_id,
        user_id=user_id,
        workspace_name=workspace_name,
        source_type="str",
        content_hash=target_digest,
        size_bytes=6,
        parser="text",
        chunk_count=1,
        artifact_path=str(target),
        created_at=now,
        modified_at=now,
    )
    sibling_resource = target_resource.model_copy(
        update={"document_id": new_id(), "content_hash": sibling_digest, "size_bytes": 4}
    )
    await metadata.add_resource(target_resource, lambda: None)
    await metadata.add_resource(sibling_resource, lambda: None)
    task = TaskRecord(
        task_id=new_task_id(),
        user_id=user_id,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        operation="delete_string",
        payload={"content_hash": target_digest, "allow_missing": False},
    )
    await metadata.create_task(task)
    chunk = ChunkRecord(
        chunk_id=chunk_id(target_resource.document_id, 0, "remove"),
        workspace_id=workspace_id,
        user_id=user_id,
        document_id=target_resource.document_id,
        source_type="str",
        ordinal=0,
        content="remove",
        content_hash=content_hash(b"remove"),
        start_offset=0,
        end_offset=6,
        embedding_text="remove",
        created_at=now,
    )
    tasks = FakeTasks()
    elasticsearch = FakeElasticsearch(chunk)
    milvus = FakeMilvus(chunk)
    runtime = SimpleNamespace(
        metadata=metadata,
        artifacts=artifacts,
        tasks=tasks,
        task_log=FakeTaskLog(),
        elasticsearch=elasticsearch,
        milvus=milvus,
    )

    await process_delete(runtime, task.task_id)

    assert await metadata.get_string(workspace_id, target_digest) is None
    assert await metadata.get_string(workspace_id, sibling_digest) is not None
    workspace = await metadata.get_workspace(workspace_id)
    users = await metadata.list_users()
    assert workspace is not None
    assert (workspace.resource_count, workspace.str_count, workspace.total_size_bytes) == (1, 1, 4)
    assert (users.users[0].resource_count, users.users[0].str_count, users.users[0].total_size_bytes) == (1, 1, 4)
    assert await artifacts.read_string_record(user_id, workspace_id, target_digest) is None
    assert await artifacts.read_string_record(user_id, workspace_id, sibling_digest) == sibling_record
    assert elasticsearch.deleted == [(workspace_id, target_resource.document_id)]
    assert milvus.deleted == [(workspace_id, target_resource.document_id)]
    assert tasks.released == [("str", target_digest)]
    completed = await metadata.get_task(task.task_id)
    assert completed is not None
    assert completed.status == TaskStatus.SUCCEEDED
    assert completed.result["content_hash"] == target_digest  # type: ignore[index]
    assert completed.result["document_id"] == str(target_resource.document_id)  # type: ignore[index]


async def test_delete_string_compensation_restores_artifact_indexes_and_metadata() -> None:
    now = local_now()
    digest = "sha256:" + "c" * 32
    resource = ResourceRecord(
        document_id=new_id(),
        workspace_id="workspace_11111111-1111-5111-8111-111111111111",
        user_id="user-001",
        workspace_name="Knowledge",
        source_type="str",
        content_hash=digest,
        size_bytes=7,
        parser="text",
        chunk_count=1,
        artifact_path="/tmp/strings.jsonl",
        created_at=now,
        modified_at=now,
    )
    chunk = ChunkRecord(
        chunk_id=chunk_id(resource.document_id, 0, "restore"),
        workspace_id=resource.workspace_id,
        user_id=resource.user_id,
        document_id=resource.document_id,
        source_type="str",
        ordinal=0,
        content="restore",
        content_hash=content_hash(b"restore"),
        start_offset=0,
        end_offset=7,
        embedding_text="restore",
        created_at=now,
    )
    record = {"content": "restore", "content_hash": digest, "size_bytes": 7}
    events = []

    class Metadata:
        async def update_task(self, task_id: str, **fields) -> None:
            return None

        async def get_resource_by_document(self, workspace_id: str, document_id):
            return None

        async def restore_resource(self, restored: ResourceRecord) -> None:
            events.append("metadata")

        async def get_workspace(self, workspace_id: str):
            return WorkspaceRecord(
                workspace_id=resource.workspace_id,
                user_id=resource.user_id,
                workspace_name=resource.workspace_name,
            )

    class Artifacts:
        async def restore_string_record(self, user_id: str, workspace_id: str, restored: dict) -> None:
            assert restored == record
            events.append("artifact")

        async def cleanup_recycle(self, task_id: str) -> None:
            return None

        async def cleanup_staging(self, task_id: str) -> None:
            return None

    class Elasticsearch:
        async def index_resource(self, restored: ResourceRecord, chunks) -> None:
            assert chunks == [chunk]
            events.append("elasticsearch")

        async def mirror_workspace(self, workspace) -> None:
            events.append("workspace")

    class Milvus:
        async def upsert_chunks(self, chunks, vectors) -> None:
            assert chunks == [chunk]
            assert vectors == [[0.1, 0.2]]
            events.append("milvus")

    task = TaskRecord(
        task_id=new_task_id(),
        user_id=resource.user_id,
        workspace_id=resource.workspace_id,
        workspace_name=resource.workspace_name,
        operation="delete_string",
        status="running",
        payload={"content_hash": digest},
    )
    runtime = SimpleNamespace(
        metadata=Metadata(),
        artifacts=Artifacts(),
        elasticsearch=Elasticsearch(),
        milvus=Milvus(),
        task_log=FakeTaskLog(),
    )
    backup = {
        "string_record": record,
        "es_chunks": [chunk.model_dump(mode="json")],
        "vector_chunks": [chunk.model_dump(mode="json")],
        "vectors": [[0.1, 0.2]],
    }

    errors = await _compensate_delete(runtime, task, resource, None, backup)

    assert errors == []
    assert events == ["artifact", "elasticsearch", "milvus", "metadata", "workspace"]
