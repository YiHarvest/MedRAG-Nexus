"""提供兼容 OpenAI 接口的嵌入与重排模型客户端。"""

from __future__ import annotations

from typing import Any

import httpx

from medrag_nexus.core.config import Settings


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _endpoint(url: str, operation: str) -> str:
    base = url.rstrip("/")
    if base.endswith(f"/{operation}"):
        return base
    if base.endswith("/v1"):
        return f"{base}/{operation}"
    return f"{base}/v1/{operation}"


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        timeout = httpx.Timeout(180.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=_headers(self.settings.openai_embedding_api_key),
            trust_env=False,
        ) as client:
            for start in range(0, len(texts), self.settings.embedding_batch_size):
                batch = texts[start : start + self.settings.embedding_batch_size]
                response = await client.post(
                    _endpoint(self.settings.openai_embedding_url, "embeddings"),
                    json={"model": self.settings.embedding_model, "input": batch},
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data", [])
                if len(data) != len(batch):
                    raise RuntimeError(f"embedding service returned {len(data)} vectors for {len(batch)} inputs")
                ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
                for item in ordered:
                    vector = item.get("embedding")
                    if not isinstance(vector, list) or len(vector) != self.settings.embedding_dimension:
                        actual = len(vector) if isinstance(vector, list) else 0
                        raise RuntimeError(
                            f"embedding dimension mismatch: expected {self.settings.embedding_dimension}, got {actual}"
                        )
                    vectors.append([float(value) for value in vector])
        return vectors

    async def health(self) -> None:
        await self.embed(["health check"])


class RerankClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.rerank_url)

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        if not documents:
            return []
        if not self.enabled:
            raise RuntimeError("rerank service is not configured")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            headers=_headers(self.settings.rerank_api_key),
            trust_env=False,
        ) as client:
            response = await client.post(
                _endpoint(self.settings.rerank_url, "rerank"),
                json={
                    "model": self.settings.rerank_model,
                    "query": query,
                    "documents": documents,
                    "top_n": len(documents),
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
        rows = payload.get("results") or payload.get("data") or []
        result: list[tuple[int, float]] = []
        for fallback_index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            index = int(row.get("index", fallback_index))
            score = row.get("relevance_score", row.get("score"))
            if score is not None and 0 <= index < len(documents):
                result.append((index, float(score)))
        if not result:
            raise RuntimeError("rerank service returned no usable scores")
        return sorted(result, key=lambda item: item[1], reverse=True)

    async def health(self) -> None:
        await self.rerank("health", ["health"])
