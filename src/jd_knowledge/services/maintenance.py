"""执行过期任务、日志与临时制品的周期性清理。"""

from __future__ import annotations

from jd_knowledge.services.runtime import Runtime


async def cleanup(runtime: Runtime) -> None:
    await runtime.metadata.cleanup_tasks(runtime.settings.task_retention_days)
    await runtime.artifacts.cleanup_stale_staging()


async def reconcile(runtime: Runtime) -> None:
    """Repair a missing search side from the surviving side; SQLite remains authoritative."""
    for resource in await runtime.metadata.all_resources():
        es_chunks = await runtime.elasticsearch.get_chunks(resource.workspace_id, resource.document_id)
        milvus_count = await runtime.milvus.count_resource(resource.workspace_id, resource.document_id)
        if len(es_chunks) == resource.chunk_count and milvus_count != resource.chunk_count:
            vectors = await runtime.embedding.embed([chunk.embedding_text for chunk in es_chunks])
            await runtime.milvus.delete_resource(resource.workspace_id, resource.document_id)
            await runtime.milvus.upsert_chunks(es_chunks, vectors)
        elif len(es_chunks) != resource.chunk_count and milvus_count == resource.chunk_count:
            chunks, _ = await runtime.milvus.get_resource_chunks(resource.workspace_id, resource.document_id)
            await runtime.elasticsearch.delete_resource(resource.workspace_id, resource.document_id)
            await runtime.elasticsearch.index_resource(resource, chunks)
        workspace = await runtime.metadata.get_workspace(resource.workspace_id)
        if workspace:
            await runtime.elasticsearch.mirror_workspace(workspace)
