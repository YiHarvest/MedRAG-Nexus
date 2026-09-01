"""验证未完成资源不会出现在列表和统计中。"""

from __future__ import annotations

from types import SimpleNamespace

from jd_knowledge.core.ids import file_id, new_id
from jd_knowledge.core.models import ResourceRecord, WorkspaceRecord, local_now
from jd_knowledge.services.files import FileService


def _resource(workspace: WorkspaceRecord, name: str) -> ResourceRecord:
    now = local_now()
    return ResourceRecord(
        document_id=new_id(),
        workspace_id=workspace.workspace_id,
        user_id=workspace.user_id,
        workspace_name=workspace.workspace_name,
        source_type="file",
        file_id=file_id(),
        file_name=name,
        mime_type="text/plain",
        content_hash="sha256:" + name.encode().hex().ljust(32, "0")[:32],
        size_bytes=10,
        markdown_hash="sha256:" + "a" * 32,
        parser="text",
        chunk_count=2,
        artifact_path=f"/data/{name}",
        created_at=now,
        modified_at=now,
    )


async def test_incomplete_resource_is_hidden_from_file_list_and_stats() -> None:
    workspace = WorkspaceRecord(
        workspace_id="workspace_1",
        user_id="user-001",
        workspace_name="Knowledge",
        resource_count=2,
        file_count=2,
        total_size_bytes=20,
    )
    complete = _resource(workspace, "complete.txt")
    incomplete = _resource(workspace, "incomplete.txt")
    task_not_succeeded = _resource(workspace, "failed-task.txt")
    warnings: list[dict[str, object]] = []

    class Metadata:
        async def get_workspace(self, workspace_id: str):
            return workspace

        async def list_resource_records(self, workspace_id: str):
            return [complete, incomplete]

        async def incomplete_resource_records(self, workspace_id: str):
            return [task_not_succeeded]

    class Artifacts:
        async def resource_is_complete(self, resource: ResourceRecord) -> bool:
            return resource.document_id == complete.document_id

    class Elasticsearch:
        async def get_resource(self, workspace_id: str, document_id):
            return complete if document_id == complete.document_id else None

        async def count_document_chunks(self, workspace_id: str, document_id) -> int:
            return 2 if document_id == complete.document_id else 0

    class Milvus:
        async def count_resource(self, workspace_id: str, document_id) -> int:
            return 2 if document_id == complete.document_id else 0

    class TaskLog:
        async def write_api(self, level: str, message: str, **context: object) -> None:
            warnings.append(context)

    runtime = SimpleNamespace(
        metadata=Metadata(),
        artifacts=Artifacts(),
        elasticsearch=Elasticsearch(),
        milvus=Milvus(),
        task_log=TaskLog(),
    )

    response = await FileService(runtime).list_files(workspace.user_id, workspace.workspace_id)

    assert [item.file_name for item in response.files] == ["complete.txt"]
    assert response.stats.resource_count == 1
    assert response.stats.file_count == 1
    assert response.stats.total_size_bytes == 10
    assert len(warnings) == 2
    by_document = {warning["document_id"]: warning for warning in warnings}
    assert by_document[incomplete.document_id]["incomplete_parts"] == (
        "artifact,elasticsearch_resource,elasticsearch_chunks,milvus_chunks"
    )
    assert by_document[task_not_succeeded.document_id]["incomplete_parts"] == "task_not_succeeded_100"
