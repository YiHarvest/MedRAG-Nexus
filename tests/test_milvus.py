"""验证 Milvus 存储适配器的写入与一致性查询。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from medrag_nexus.storage.milvus import MilvusStore


class FakeMilvusClient:
    def __init__(self) -> None:
        self.upsert_kwargs: dict[str, Any] | None = None
        self.query_kwargs: dict[str, Any] | None = None
        self.search_kwargs: dict[str, Any] | None = None

    def upsert(self, **kwargs: Any) -> dict[str, int]:
        self.upsert_kwargs = kwargs
        return {"upsert_count": len(kwargs["data"])}

    def query(self, **kwargs: Any) -> list[dict[str, int]]:
        self.query_kwargs = kwargs
        return [{"count(*)": 1}]

    def search(self, **kwargs: Any) -> list[list[dict[str, Any]]]:
        self.search_kwargs = kwargs
        return [[]]


def store_with_fake_client() -> tuple[MilvusStore, FakeMilvusClient]:
    store = object.__new__(MilvusStore)
    client = FakeMilvusClient()
    store.client = client
    store.settings = SimpleNamespace(milvus_collection="knowledge_chunks")
    return store, client


@pytest.mark.asyncio
async def test_upsert_chunks_uses_upsert_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    store, client = store_with_fake_client()
    rows = [{"chunk_id": "chunk-1"}, {"chunk_id": "chunk-2"}]
    monkeypatch.setattr(store, "_rows", lambda chunks, vectors: rows)

    inserted = await store.upsert_chunks([object(), object()], [[0.1], [0.2]])  # type: ignore[list-item]

    assert inserted == 2
    assert client.upsert_kwargs is not None
    assert client.upsert_kwargs["data"] == rows


@pytest.mark.asyncio
async def test_count_resource_uses_strong_consistency() -> None:
    store, client = store_with_fake_client()

    count = await store.count_resource("workspace_11111111-1111-5111-8111-111111111111", uuid4())

    assert count == 1
    assert client.query_kwargs is not None
    assert client.query_kwargs["consistency_level"] == "Strong"


@pytest.mark.asyncio
async def test_vector_search_uses_strong_consistency() -> None:
    store, client = store_with_fake_client()

    results = await store.vector_search("workspace_11111111-1111-5111-8111-111111111111", [0.1, 0.2], 10)

    assert results == []
    assert client.search_kwargs is not None
    assert client.search_kwargs["consistency_level"] == "Strong"
