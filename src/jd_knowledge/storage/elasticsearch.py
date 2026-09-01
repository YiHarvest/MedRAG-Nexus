"""封装 Elasticsearch 中的 Workspace 元数据与关键词索引操作。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from elasticsearch import AsyncElasticsearch, NotFoundError
from elasticsearch.helpers import async_bulk

from jd_knowledge.core.config import Settings
from jd_knowledge.core.models import ChunkRecord, ResourceRecord, WorkspaceRecord


def _body(response: Any) -> Any:
    return response.body if hasattr(response, "body") else response


def _jsonable(model: Any) -> dict[str, Any]:
    return json.loads(model.model_dump_json(exclude_none=True))


def _chunk_from_source(source: dict[str, Any]) -> ChunkRecord:
    return ChunkRecord.model_validate(source)


class ElasticsearchStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        kwargs: dict[str, Any] = {"hosts": [settings.elasticsearch_url], "request_timeout": 30}
        if settings.elasticsearch_api_key:
            kwargs["api_key"] = settings.elasticsearch_api_key
        elif settings.elasticsearch_username:
            kwargs["basic_auth"] = (settings.elasticsearch_username, settings.elasticsearch_password)
        self.client = AsyncElasticsearch(**kwargs)

    async def close(self) -> None:
        await self.client.close()

    async def delete_indices(self, names: list[str]) -> None:
        for name in names:
            await self.client.options(ignore_status=[400, 404]).indices.delete(index=name)

    async def ensure_indices(self) -> None:
        common = {"settings": {"number_of_shards": 1, "number_of_replicas": 0}}
        definitions = {
            self.settings.elasticsearch_workspace_index: {
                **common,
                "mappings": {
                    "properties": {
                        "workspace_id": {"type": "keyword"},
                        "user_id": {"type": "keyword"},
                        "workspace_name": {"type": "keyword"},
                        "resource_count": {"type": "long"},
                        "file_count": {"type": "long"},
                        "str_count": {"type": "long"},
                        "total_size_bytes": {"type": "long"},
                        "created_at": {"type": "date"},
                        "modified_at": {"type": "date"},
                    }
                },
            },
            self.settings.elasticsearch_document_index: {
                **common,
                "mappings": {
                    "properties": {
                        "document_id": {"type": "keyword"},
                        "workspace_id": {"type": "keyword"},
                        "user_id": {"type": "keyword"},
                        "workspace_name": {"type": "keyword"},
                        "source_type": {"type": "keyword"},
                        "file_id": {"type": "keyword"},
                        "file_name": {"type": "keyword"},
                        "content_hash": {"type": "keyword"},
                        "size_bytes": {"type": "long"},
                        "created_at": {"type": "date"},
                        "modified_at": {"type": "date"},
                    }
                },
            },
            self.settings.elasticsearch_chunk_index: {
                **common,
                "mappings": {
                    "properties": {
                        "chunk_id": {"type": "keyword"},
                        "workspace_id": {"type": "keyword"},
                        "user_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "source_type": {"type": "keyword"},
                        "file_id": {"type": "keyword"},
                        "file_name": {"type": "keyword"},
                        "ordinal": {"type": "integer"},
                        "content": {"type": "text", "analyzer": "standard"},
                        "content_hash": {"type": "keyword"},
                        "section": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                        "page_number": {"type": "integer"},
                        "chunk_type": {"type": "keyword"},
                        "start_offset": {"type": "integer"},
                        "end_offset": {"type": "integer"},
                        "created_at": {"type": "date"},
                    }
                },
            },
        }
        for index, definition in definitions.items():
            if not await self.client.indices.exists(index=index):
                await self.client.options(ignore_status=400).indices.create(index=index, **definition)

    async def health(self) -> None:
        response = _body(await self.client.cluster.health(local=True))
        if response.get("status") == "red":
            raise RuntimeError("Elasticsearch cluster health is red")

    async def mirror_workspace(self, workspace: WorkspaceRecord) -> None:
        await self.client.index(
            index=self.settings.elasticsearch_workspace_index,
            id=workspace.workspace_id,
            document=_jsonable(workspace),
            refresh="wait_for",
        )

    async def delete_workspace(self, workspace_id: str) -> None:
        await self.client.options(ignore_status=404).delete(
            index=self.settings.elasticsearch_workspace_index,
            id=workspace_id,
            refresh="wait_for",
        )

    async def rename_workspace(self, workspace: WorkspaceRecord) -> None:
        """Keep denormalized workspace names in sync without touching chunk payloads."""

        await self.mirror_workspace(workspace)
        await self.client.update_by_query(
            index=self.settings.elasticsearch_document_index,
            query={"term": {"workspace_id": workspace.workspace_id}},
            script={
                "source": "ctx._source.workspace_name = params.workspace_name",
                "lang": "painless",
                "params": {"workspace_name": workspace.workspace_name},
            },
            refresh=True,
            conflicts="proceed",
        )

    async def delete_workspace_contents(self, workspace_id: str) -> None:
        """Remove all search metadata owned by one workspace."""

        query = {"term": {"workspace_id": workspace_id}}
        for index in (
            self.settings.elasticsearch_chunk_index,
            self.settings.elasticsearch_document_index,
        ):
            await self.client.delete_by_query(
                index=index,
                query=query,
                refresh=True,
                conflicts="proceed",
            )
        await self.delete_workspace(workspace_id)

    async def count_workspace_contents(self, workspace_id: str) -> tuple[int, int]:
        query = {"term": {"workspace_id": workspace_id}}
        documents = _body(
            await self.client.count(index=self.settings.elasticsearch_document_index, query=query)
        )
        chunks = _body(await self.client.count(index=self.settings.elasticsearch_chunk_index, query=query))
        return int(documents.get("count", 0)), int(chunks.get("count", 0))

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        try:
            response = await self.client.get(index=self.settings.elasticsearch_workspace_index, id=workspace_id)
        except NotFoundError:
            return None
        return WorkspaceRecord.model_validate(_body(response)["_source"])

    async def index_resource(self, resource: ResourceRecord, chunks: list[ChunkRecord]) -> None:
        actions = [
            {
                "_index": self.settings.elasticsearch_chunk_index,
                "_id": str(chunk.chunk_id),
                "_source": _jsonable(chunk),
            }
            for chunk in chunks
        ]
        if actions:
            _, errors = await async_bulk(self.client, actions, refresh="wait_for", raise_on_error=False)
            if errors:
                raise RuntimeError(f"Elasticsearch chunk bulk failed: {errors[:3]}")
        await self.client.index(
            index=self.settings.elasticsearch_document_index,
            id=str(resource.document_id),
            document=_jsonable(resource),
            refresh="wait_for",
        )
        count = await self.count_document_chunks(resource.workspace_id, resource.document_id)
        if count != len(chunks):
            raise RuntimeError(f"Elasticsearch chunk count mismatch: expected={len(chunks)}, actual={count}")

    async def count_document_chunks(self, workspace_id: str, document_id: UUID) -> int:
        response = _body(
            await self.client.count(
                index=self.settings.elasticsearch_chunk_index,
                query={
                    "bool": {
                        "filter": [
                            {"term": {"workspace_id": workspace_id}},
                            {"term": {"document_id": str(document_id)}},
                        ]
                    }
                },
            )
        )
        return int(response.get("count", 0))

    async def delete_resource(self, workspace_id: str, document_id: UUID) -> None:
        await self.client.delete_by_query(
            index=self.settings.elasticsearch_chunk_index,
            query={
                "bool": {
                    "filter": [
                        {"term": {"workspace_id": workspace_id}},
                        {"term": {"document_id": str(document_id)}},
                    ]
                }
            },
            refresh=True,
            conflicts="proceed",
        )
        await self.client.options(ignore_status=404).delete(
            index=self.settings.elasticsearch_document_index,
            id=str(document_id),
            refresh="wait_for",
        )

    async def get_resource(self, workspace_id: str, document_id: UUID) -> ResourceRecord | None:
        try:
            response = await self.client.get(index=self.settings.elasticsearch_document_index, id=str(document_id))
        except NotFoundError:
            return None
        source = _body(response)["_source"]
        return ResourceRecord.model_validate(source) if source.get("workspace_id") == workspace_id else None

    async def get_chunks(self, workspace_id: str, document_id: UUID) -> list[ChunkRecord]:
        response = _body(
            await self.client.search(
                index=self.settings.elasticsearch_chunk_index,
                size=10_000,
                query={
                    "bool": {
                        "filter": [
                            {"term": {"workspace_id": workspace_id}},
                            {"term": {"document_id": str(document_id)}},
                        ]
                    }
                },
                sort=[{"ordinal": "asc"}],
            )
        )
        return [_chunk_from_source(hit["_source"]) for hit in response.get("hits", {}).get("hits", [])]

    async def keyword_search(self, workspace_id: str, query: str, limit: int) -> list[tuple[ChunkRecord, float]]:
        response = _body(
            await self.client.search(
                index=self.settings.elasticsearch_chunk_index,
                size=limit,
                query={
                    "bool": {
                        "filter": [{"term": {"workspace_id": workspace_id}}],
                        "must": [{"multi_match": {"query": query, "fields": ["content^2", "section"]}}],
                    }
                },
            )
        )
        return [
            (_chunk_from_source(hit["_source"]), float(hit.get("_score") or 0.0))
            for hit in response.get("hits", {}).get("hits", [])
        ]
