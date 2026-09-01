"""验证混合检索响应、候选过滤和日志记录。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jd_knowledge.core.ids import content_hash, new_id
from jd_knowledge.core.models import ChunkRecord, RetrievalRequest
from jd_knowledge.services.retrieval import retrieve

WORKSPACE_ID = "workspace_11111111-1111-5111-8111-111111111111"


class FakeSettings:
    retrieval_default_top_k = 10
    retrieval_max_top_k = 50
    rerank_max_candidates = 200
    rrf_k = 60
    dependency_timeout_seconds = 0.2

    @staticmethod
    def candidate_k(top_k: int) -> int:
        return top_k


class FakeMetadata:
    async def get_workspace(self, workspace_id: str) -> SimpleNamespace:
        assert workspace_id == WORKSPACE_ID
        return SimpleNamespace(user_id="user-003")

    async def existing_document_ids(self, workspace_id: str, document_ids: set) -> set:
        assert workspace_id == WORKSPACE_ID
        return document_ids


class FakeElasticsearch:
    def __init__(self, chunk: ChunkRecord) -> None:
        self.chunk = chunk

    async def keyword_search(self, workspace_id: str, query: str, limit: int) -> list[tuple[ChunkRecord, float]]:
        return [(self.chunk, 2.5)]


class FakeEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2]]


class SlowEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(1)
        return [[0.1, 0.2]]


class FakeMilvus:
    def __init__(self, chunk: ChunkRecord) -> None:
        self.chunk = chunk

    async def vector_search(
        self, workspace_id: str, vector: list[float], limit: int
    ) -> list[tuple[ChunkRecord, float]]:
        return [(self.chunk, 0.8)]


class FakeTaskLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def write_retrieval(self, level: str, message: str, **context: object) -> None:
        self.events.append((level, message, context))


async def test_retrieval_response_exposes_source_chunk_id() -> None:
    chunk = ChunkRecord(
        chunk_id=new_id(),
        workspace_id=WORKSPACE_ID,
        user_id="user-003",
        document_id=new_id(),
        source_type="file",
        file_id="file_11111111-1111-4111-8111-111111111111",
        file_name="prompt.txt",
        ordinal=0,
        content="提示词内容",
        content_hash=content_hash("提示词内容".encode()),
        start_offset=0,
        end_offset=5,
        embedding_text="提示词内容",
    )
    task_log = FakeTaskLog()
    runtime = SimpleNamespace(
        settings=FakeSettings(),
        metadata=FakeMetadata(),
        elasticsearch=FakeElasticsearch(chunk),
        embedding=FakeEmbedding(),
        milvus=FakeMilvus(chunk),
        rerank=SimpleNamespace(enabled=False),
        task_log=task_log,
    )

    response = await retrieve(
        runtime,
        RetrievalRequest(user_id="user-003", workspace_id=WORKSPACE_ID, query="提示词\n第二行", top_k=1),
    )

    assert response.items[0].chunk_id == chunk.chunk_id
    assert response.model_dump(mode="json")["items"][0]["chunk_id"] == str(chunk.chunk_id)
    assert task_log.events[0][1] == "收到用户检索问题"
    assert task_log.events[0][2]["query"] == "提示词\n第二行"
    assert {message for _, message, _ in task_log.events} >= {
        "开始并行执行 BM25 检索与问题向量化",
        "Elasticsearch BM25 召回完成",
        "Milvus 向量召回完成",
        "候选文档有效性过滤与 RRF 融合完成",
        "检索完成（部分通道降级）",
    }


async def test_retrieval_returns_keyword_results_when_vector_channel_times_out() -> None:
    chunk = ChunkRecord(
        chunk_id=new_id(),
        workspace_id=WORKSPACE_ID,
        user_id="user-003",
        document_id=new_id(),
        source_type="str",
        ordinal=0,
        content="关键词命中的内容",
        content_hash=content_hash("关键词命中的内容".encode()),
        start_offset=0,
        end_offset=8,
        embedding_text="关键词命中的内容",
    )
    settings = FakeSettings()
    settings.dependency_timeout_seconds = 0.01
    runtime = SimpleNamespace(
        settings=settings,
        metadata=FakeMetadata(),
        elasticsearch=FakeElasticsearch(chunk),
        embedding=SlowEmbedding(),
        milvus=FakeMilvus(chunk),
        rerank=SimpleNamespace(enabled=False),
        task_log=FakeTaskLog(),
    )

    response = await retrieve(
        runtime,
        RetrievalRequest(user_id="user-003", workspace_id=WORKSPACE_ID, query="关键词", top_k=1),
    )

    assert response.count == 1
    assert response.items[0].content == "关键词命中的内容"
    assert response.degraded is True
    assert {warning.code for warning in response.warnings} >= {"vector_timeout"}
