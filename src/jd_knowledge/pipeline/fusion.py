"""融合向量搜索和关键词搜索产生的候选结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from jd_knowledge.core.models import ChunkRecord


@dataclass(slots=True)
class SearchCandidate:
    chunk: ChunkRecord
    vector_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None
    matched_by: set[Literal["vector", "bm25"]] = field(default_factory=set)


def reciprocal_rank_fusion(
    vector_results: list[tuple[ChunkRecord, float]],
    keyword_results: list[tuple[ChunkRecord, float]],
    *,
    rrf_k: int,
) -> list[SearchCandidate]:
    merged: dict[UUID, SearchCandidate] = {}
    for method, rows in (("vector", vector_results), ("bm25", keyword_results)):
        for rank, (chunk, score) in enumerate(rows, start=1):
            candidate = merged.setdefault(chunk.chunk_id, SearchCandidate(chunk=chunk))
            candidate.rrf_score += 1.0 / (rrf_k + rank)
            if method == "vector":
                candidate.vector_score = score
                candidate.matched_by.add("vector")
            else:
                candidate.bm25_score = score
                candidate.matched_by.add("bm25")
    return sorted(merged.values(), key=lambda item: item.rrf_score, reverse=True)
