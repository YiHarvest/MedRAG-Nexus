"""封装 Milvus 向量集合的写入、查询与健康检查。"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from jd_knowledge.core.config import Settings
from jd_knowledge.core.models import ChunkRecord


class MilvusStore:
    def __init__(self, settings: Settings):
        from pymilvus import MilvusClient

        self.settings = settings
        kwargs: dict[str, Any] = {"uri": f"http://{settings.milvus_host}:{settings.milvus_port}"}
        if settings.milvus_token:
            kwargs["token"] = settings.milvus_token
        self.client = MilvusClient(**kwargs)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    async def drop_collection(self, name: str) -> None:
        if await asyncio.to_thread(self.client.has_collection, collection_name=name):
            await asyncio.to_thread(self.client.drop_collection, collection_name=name)

    async def ensure_collection(self) -> None:
        from pymilvus import DataType

        name = self.settings.milvus_collection
        if await asyncio.to_thread(self.client.has_collection, collection_name=name):
            return

        def create() -> None:
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("workspace_id", DataType.VARCHAR, max_length=64, is_partition_key=True)
            schema.add_field("user_id", DataType.VARCHAR, max_length=128)
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("source_type", DataType.VARCHAR, max_length=16)
            schema.add_field("file_id", DataType.VARCHAR, max_length=64)
            schema.add_field("file_name", DataType.VARCHAR, max_length=512)
            schema.add_field("ordinal", DataType.INT64)
            schema.add_field("content", DataType.VARCHAR, max_length=65535)
            schema.add_field("content_hash", DataType.VARCHAR, max_length=48)
            schema.add_field("section", DataType.VARCHAR, max_length=2048)
            schema.add_field("page_number", DataType.INT64)
            schema.add_field("chunk_type", DataType.VARCHAR, max_length=32)
            schema.add_field("start_offset", DataType.INT64)
            schema.add_field("end_offset", DataType.INT64)
            schema.add_field("embedding_text", DataType.VARCHAR, max_length=65535)
            schema.add_field("created_at", DataType.VARCHAR, max_length=64)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.settings.embedding_dimension)
            index_params = self.client.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
            self.client.create_collection(
                collection_name=name,
                schema=schema,
                index_params=index_params,
                consistency_level="Strong",
            )

        try:
            await asyncio.to_thread(create)
        except Exception:
            if not await asyncio.to_thread(self.client.has_collection, collection_name=name):
                raise

    def _rows(self, chunks: list[ChunkRecord], vectors: list[list[float]]) -> list[dict[str, Any]]:
        if len(chunks) != len(vectors):
            raise ValueError("chunk/vector count mismatch")
        rows = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            payload = json.loads(chunk.model_dump_json(exclude={"vector"}))
            payload["chunk_id"] = str(chunk.chunk_id)
            payload["document_id"] = str(chunk.document_id)
            payload["file_id"] = str(chunk.file_id) if chunk.file_id else ""
            payload["file_name"] = chunk.file_name or ""
            payload["section"] = chunk.section or ""
            payload["page_number"] = chunk.page_number or 0
            payload["created_at"] = chunk.created_at.isoformat()
            payload["vector"] = vector
            rows.append(payload)
        return rows

    async def upsert_chunks(self, chunks: list[ChunkRecord], vectors: list[list[float]]) -> int:
        rows = self._rows(chunks, vectors)
        if not rows:
            return 0
        result = await asyncio.to_thread(
            self.client.upsert,
            collection_name=self.settings.milvus_collection,
            data=rows,
        )
        return int(result.get("upsert_count", result.get("insert_count", 0)))

    async def delete_resource(self, workspace_id: str, document_id: UUID) -> None:
        await asyncio.to_thread(
            self.client.delete,
            collection_name=self.settings.milvus_collection,
            filter=f'workspace_id == "{workspace_id}" and document_id == "{document_id}"',
        )

    async def delete_workspace(self, workspace_id: str) -> None:
        await asyncio.to_thread(
            self.client.delete,
            collection_name=self.settings.milvus_collection,
            filter=f'workspace_id == "{workspace_id}"',
        )

    async def count_workspace(self, workspace_id: str) -> int:
        rows = await asyncio.to_thread(
            self.client.query,
            collection_name=self.settings.milvus_collection,
            filter=f'workspace_id == "{workspace_id}"',
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        return int(rows[0].get("count(*)", 0)) if rows else 0

    async def count_resource(self, workspace_id: str, document_id: UUID) -> int:
        rows = await asyncio.to_thread(
            self.client.query,
            collection_name=self.settings.milvus_collection,
            filter=f'workspace_id == "{workspace_id}" and document_id == "{document_id}"',
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        return int(rows[0].get("count(*)", 0)) if rows else 0

    async def vector_search(
        self,
        workspace_id: str,
        vector: list[float],
        limit: int,
    ) -> list[tuple[ChunkRecord, float]]:
        output_fields = [
            "chunk_id",
            "workspace_id",
            "user_id",
            "document_id",
            "source_type",
            "file_id",
            "file_name",
            "ordinal",
            "content",
            "content_hash",
            "section",
            "page_number",
            "chunk_type",
            "start_offset",
            "end_offset",
            "embedding_text",
            "created_at",
        ]
        results = await asyncio.to_thread(
            self.client.search,
            collection_name=self.settings.milvus_collection,
            data=[vector],
            filter=f'workspace_id == "{workspace_id}"',
            limit=limit,
            output_fields=output_fields,
            search_params={"metric_type": "COSINE"},
            consistency_level="Strong",
        )
        rows: list[tuple[ChunkRecord, float]] = []
        for hit in results[0] if results else []:
            entity = dict(hit.get("entity") or {})
            entity["file_id"] = entity.get("file_id") or None
            entity["file_name"] = entity.get("file_name") or None
            entity["section"] = entity.get("section") or None
            entity["page_number"] = entity.get("page_number") or None
            rows.append((ChunkRecord.model_validate(entity), float(hit.get("distance", hit.get("score", 0.0)))))
        return rows

    async def get_resource_chunks(
        self,
        workspace_id: str,
        document_id: UUID,
    ) -> tuple[list[ChunkRecord], list[list[float]]]:
        rows = await asyncio.to_thread(
            self.client.query,
            collection_name=self.settings.milvus_collection,
            filter=f'workspace_id == "{workspace_id}" and document_id == "{document_id}"',
            output_fields=["*", "vector"],
            consistency_level="Strong",
        )
        chunks: list[ChunkRecord] = []
        vectors: list[list[float]] = []
        for row in rows:
            payload = dict(row)
            vector = payload.pop("vector")
            payload["file_id"] = payload.get("file_id") or None
            payload["file_name"] = payload.get("file_name") or None
            payload["section"] = payload.get("section") or None
            payload["page_number"] = payload.get("page_number") or None
            chunks.append(ChunkRecord.model_validate(payload))
            vectors.append([float(value) for value in vector])
        return chunks, vectors

    async def health(self) -> None:
        await asyncio.to_thread(self.client.list_collections)
