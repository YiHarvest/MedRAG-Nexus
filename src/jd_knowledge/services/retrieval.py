"""执行 Workspace 范围内的混合知识检索。"""

from __future__ import annotations

import asyncio
import time

from jd_knowledge.core.models import (
    DomainError,
    RetrievalItem,
    RetrievalRequest,
    RetrievalResponse,
    RetrievalScores,
    WarningItem,
)
from jd_knowledge.pipeline.fusion import reciprocal_rank_fusion
from jd_knowledge.services.runtime import Runtime


async def _log_retrieval(runtime: Runtime, level: str, message: str, **context: object) -> None:
    """安全写入检索日志：日志设施缺失或失败时不破坏检索主流程。"""
    task_log = getattr(runtime, "task_log", None)
    if task_log is None:
        return
    try:
        await task_log.write_retrieval(level, message, **context)
    except Exception:
        return


async def retrieve(runtime: Runtime, request: RetrievalRequest) -> RetrievalResponse:
    top_k = request.top_k or runtime.settings.retrieval_default_top_k
    started = time.monotonic()
    base_context = {
        "user_id": request.user_id,
        "workspace_id": request.workspace_id,
        "query": request.query,
        "top_k": top_k,
    }
    await _log_retrieval(runtime, "INFO", "收到用户检索问题", **base_context)
    try:
        return await _retrieve(runtime, request, top_k, started, base_context)
    except Exception as exc:
        await _log_retrieval(
            runtime,
            "ERROR",
            "检索失败",
            exception_type=type(exc).__name__,
            error=str(exc)[:500],
            elapsed_ms=round((time.monotonic() - started) * 1000),
            **base_context,
        )
        raise


async def _retrieve(
    runtime: Runtime,
    request: RetrievalRequest,
    top_k: int,
    started: float,
    base_context: dict[str, object],
) -> RetrievalResponse:
    if top_k > runtime.settings.retrieval_max_top_k:
        raise DomainError(
            "invalid_top_k",
            f"top_k must be <= {runtime.settings.retrieval_max_top_k}",
            status_code=422,
        )
    candidate_k = runtime.settings.candidate_k(top_k)
    workspace = await runtime.metadata.get_workspace(request.workspace_id)
    if workspace is None or workspace.user_id != request.user_id:
        raise DomainError("workspace_not_found", "workspace does not exist", status_code=404)
    await _log_retrieval(
        runtime,
        "INFO",
        "Workspace 校验完成，准备混合召回",
        user_id=request.user_id,
        workspace_id=request.workspace_id,
        candidate_k=candidate_k,
        vector_enabled=True,
        bm25_enabled=True,
        rerank_enabled=runtime.rerank.enabled,
    )
    warnings: list[WarningItem] = []

    channels_started = time.monotonic()
    await _log_retrieval(
        runtime,
        "INFO",
        "开始并行执行 BM25 检索与问题向量化",
        user_id=request.user_id,
        workspace_id=request.workspace_id,
        candidate_k=candidate_k,
    )
    timeout_seconds = runtime.settings.dependency_timeout_seconds

    async def vector_search():
        query_vectors = await runtime.embedding.embed([request.query])
        if not query_vectors:
            raise RuntimeError("embedding service returned no query vector")
        await _log_retrieval(
            runtime,
            "INFO",
            "用户问题向量化完成，开始查询 Milvus",
            workspace_id=request.workspace_id,
            vector_dimension=len(query_vectors[0]),
        )
        results = await runtime.milvus.vector_search(request.workspace_id, query_vectors[0], candidate_k)
        await _log_retrieval(
            runtime,
            "INFO",
            "Milvus 向量召回完成",
            workspace_id=request.workspace_id,
            result_count=len(results),
            elapsed_ms=round((time.monotonic() - channels_started) * 1000),
        )
        return results

    keyword_task = asyncio.create_task(
        asyncio.wait_for(
            runtime.elasticsearch.keyword_search(request.workspace_id, request.query, candidate_k),
            timeout=timeout_seconds,
        )
    )
    vector_task = asyncio.create_task(asyncio.wait_for(vector_search(), timeout=timeout_seconds))
    keyword_results = []
    vector_results = []
    keyword_ok = vector_ok = False
    try:
        keyword_results = await keyword_task
        keyword_ok = True
        await _log_retrieval(
            runtime,
            "INFO",
            "Elasticsearch BM25 召回完成",
            workspace_id=request.workspace_id,
            result_count=len(keyword_results),
            elapsed_ms=round((time.monotonic() - channels_started) * 1000),
        )
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError)
        warnings.append(
            WarningItem(
                code="elasticsearch_timeout" if timed_out else "elasticsearch_unavailable",
                message="关键词检索超时，已使用向量结果" if timed_out else "关键词检索不可用，已使用向量结果",
            )
        )
        await _log_retrieval(
            runtime,
            "WARN",
            "Elasticsearch BM25 召回失败",
            workspace_id=request.workspace_id,
            exception_type=type(exc).__name__,
            error=str(exc)[:500],
        )
    try:
        vector_results = await vector_task
        vector_ok = True
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError)
        warnings.append(
            WarningItem(
                code="vector_timeout" if timed_out else "vector_unavailable",
                message="向量检索超时，已使用关键词结果" if timed_out else "向量检索不可用，已使用关键词结果",
            )
        )
        await _log_retrieval(
            runtime,
            "WARN",
            "问题向量化或 Milvus 召回失败",
            workspace_id=request.workspace_id,
            exception_type=type(exc).__name__,
            error=str(exc)[:500],
        )
    if not keyword_ok and not vector_ok:
        raise DomainError("retrieval_unavailable", "both retrieval paths are unavailable", status_code=503)

    document_ids = {chunk.document_id for chunk, _ in keyword_results + vector_results}
    existing = await runtime.metadata.existing_document_ids(request.workspace_id, document_ids)
    keyword_results = [(chunk, score) for chunk, score in keyword_results if chunk.document_id in existing]
    vector_results = [(chunk, score) for chunk, score in vector_results if chunk.document_id in existing]
    if not keyword_ok and not vector_ok:
        raise DomainError("retrieval_unavailable", "both retrieval paths are unavailable", status_code=503)

    fused = reciprocal_rank_fusion(vector_results, keyword_results, rrf_k=runtime.settings.rrf_k)
    await _log_retrieval(
        runtime,
        "INFO",
        "候选文档有效性过滤与 RRF 融合完成",
        workspace_id=request.workspace_id,
        bm25_valid_count=len(keyword_results),
        vector_valid_count=len(vector_results),
        fused_count=len(fused),
        rrf_k=runtime.settings.rrf_k,
    )
    rerank_pool = fused[: runtime.settings.rerank_max_candidates]
    if rerank_pool and runtime.rerank.enabled:
        rerank_started = time.monotonic()
        await _log_retrieval(
            runtime,
            "INFO",
            "开始执行 Rerank 精排",
            workspace_id=request.workspace_id,
            candidate_count=len(rerank_pool),
        )
        try:
            scores = await asyncio.wait_for(
                runtime.rerank.rerank(request.query, [candidate.chunk.content for candidate in rerank_pool]),
                timeout=timeout_seconds,
            )
            ranked = []
            for index, score in scores:
                candidate = rerank_pool[index]
                candidate.rerank_score = score
                ranked.append(candidate)
            rerank_pool = ranked
            await _log_retrieval(
                runtime,
                "INFO",
                "Rerank 精排完成",
                workspace_id=request.workspace_id,
                result_count=len(rerank_pool),
                elapsed_ms=round((time.monotonic() - rerank_started) * 1000),
            )
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError)
            warnings.append(
                WarningItem(
                    code="rerank_timeout" if timed_out else "rerank_unavailable",
                    message="精排超时，已保留融合排序" if timed_out else "精排不可用，已保留融合排序",
                )
            )
            await _log_retrieval(
                runtime,
                "WARN",
                "Rerank 精排失败，保留 RRF 顺序",
                workspace_id=request.workspace_id,
                exception_type=type(exc).__name__,
                error=str(exc)[:500],
            )
    elif rerank_pool:
        warnings.append(
            WarningItem(code="rerank_unconfigured", message="精排未配置，已使用融合排序")
        )
        await _log_retrieval(
            runtime,
            "WARN",
            "Rerank 未配置，使用 RRF 排序结果",
            workspace_id=request.workspace_id,
            candidate_count=len(rerank_pool),
        )

    selected = rerank_pool[:top_k]
    items = [
        RetrievalItem(
            rank=rank,
            chunk_id=candidate.chunk.chunk_id,
            user_id=candidate.chunk.user_id,
            workspace_id=candidate.chunk.workspace_id,
            source_type=candidate.chunk.source_type,
            file_id=candidate.chunk.file_id,
            file_name=candidate.chunk.file_name,
            content=candidate.chunk.content,
            section=candidate.chunk.section,
            page_number=candidate.chunk.page_number,
            scores=RetrievalScores(
                vector=candidate.vector_score,
                bm25=candidate.bm25_score,
                rrf=candidate.rrf_score,
                rerank=candidate.rerank_score,
            ),
            matched_by=sorted(candidate.matched_by),  # type: ignore[arg-type]
        )
        for rank, candidate in enumerate(selected, start=1)
    ]
    response = RetrievalResponse(
        query=request.query,
        top_k=top_k,
        count=len(items),
        degraded=bool(warnings),
        warnings=warnings,
        items=items,
    )
    await _log_retrieval(
        runtime,
        "INFO" if not warnings else "WARN",
        "检索完成" if not warnings else "检索完成（部分通道降级）",
        count=len(items),
        degraded=bool(warnings),
        warnings=",".join(w.code for w in warnings) or "-",
        result_chunk_ids=",".join(str(item.chunk_id) for item in items) or "-",
        result_files=",".join(item.file_name or "<string>" for item in items) or "-",
        elapsed_ms=round((time.monotonic() - started) * 1000),
        **base_context,
    )
    return response
