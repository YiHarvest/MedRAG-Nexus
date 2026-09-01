"""验证倒数排名融合的候选合并和来源追踪。"""

from __future__ import annotations

from medrag_nexus.core.ids import content_hash, new_id
from medrag_nexus.core.models import ChunkRecord
from medrag_nexus.pipeline.fusion import reciprocal_rank_fusion


def make_chunk(content: str, ordinal: int) -> ChunkRecord:
    document_id = new_id()
    return ChunkRecord(
        chunk_id=new_id(),
        workspace_id="workspace_11111111-1111-5111-8111-111111111111",
        user_id="u",
        document_id=document_id,
        source_type="str",
        ordinal=ordinal,
        content=content,
        content_hash=content_hash(content.encode()),
        start_offset=0,
        end_offset=len(content),
        embedding_text=content,
    )


def test_rrf_merges_same_chunk_and_tracks_sources() -> None:
    common = make_chunk("common", 0)
    vector_only = make_chunk("vector", 1)
    keyword_only = make_chunk("keyword", 2)
    fused = reciprocal_rank_fusion(
        [(common, 0.9), (vector_only, 0.8)],
        [(common, 8.0), (keyword_only, 7.0)],
        rrf_k=60,
    )
    assert fused[0].chunk.chunk_id == common.chunk_id
    assert fused[0].matched_by == {"vector", "bm25"}
    assert len(fused) == 3
