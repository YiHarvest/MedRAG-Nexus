"""Validate the resource upload HTTP boundary."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from medrag_nexus.knowledge.uploads import UploadParseError, parse_resource_upload


def _app(max_file_bytes: int = 1024) -> FastAPI:
    app = FastAPI()

    @app.post("/upload")
    async def upload(request: Request):
        try:
            parsed = await parse_resource_upload(request, max_file_bytes=max_file_bytes)
        except UploadParseError as exc:
            return {"error": exc.code, "status": exc.status_code, "details": exc.details}
        return {"type": parsed.source_type, "source": parsed.source.model_dump()}

    return app


@pytest.mark.parametrize("mime_type", ["text/markdown", "text/x-markdown"])
async def test_parse_markdown_upload(mime_type: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/upload",
            data={"type": "file"},
            files={"file": ("notes.md", b"# Notes", mime_type)},
        )

    assert response.json()["source"]["file_name"] == "notes.md"


async def test_parse_text_form_preserves_content() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post("/upload", data={"type": "str", "content": "  exact text  "})

    assert response.json()["source"]["content"] == "  exact text  "


async def test_upload_rejects_unknown_and_mutually_exclusive_fields() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        unknown = await client.post("/upload", data={"type": "str", "content": "x", "extra": "x"})
        mixed = await client.post(
            "/upload",
            data={"type": "file", "content": "not allowed"},
            files={"file": ("notes.md", b"# Notes", "text/markdown")},
        )

    assert unknown.json() == {"error": "validation_error", "status": 422, "details": {"fields": ["extra"]}}
    assert mixed.json()["error"] == "validation_error"


async def test_upload_reads_in_chunks_and_enforces_limit() -> None:
    async with AsyncClient(transport=ASGITransport(app=_app(max_file_bytes=4)), base_url="http://test") as client:
        response = await client.post(
            "/upload",
            data={"type": "file"},
            files={"file": ("notes.md", b"12345", "text/markdown")},
        )

    assert response.json()["status"] == 413
