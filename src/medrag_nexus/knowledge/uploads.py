"""Parse and validate resource upload forms at the HTTP boundary."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from fastapi import Request
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile

from medrag_nexus.core.models import FileSource, StringSource

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
_ALLOWED_FIELDS = frozenset({"type", "file", "content"})


class UploadParseError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


@dataclass(frozen=True, slots=True)
class ParsedResourceUpload:
    source_type: Literal["file", "str"]
    source: FileSource | StringSource
    audit: dict[str, object]


def _text_field(form: FormData, key: str, *, preserve: bool = False) -> str | None:
    value = form.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise UploadParseError("validation_error", f"multipart field '{key}' must be text")
    return value if preserve else (value.strip() or None)


async def parse_resource_upload(
    request: Request,
    *,
    max_file_bytes: int,
    authorize: Callable[[Literal["file", "str"]], Awaitable[None]] | None = None,
) -> ParsedResourceUpload:
    """Parse one file or text source without leaking transport details into the router."""

    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type not in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        raise UploadParseError(
            "unsupported_media_type",
            "Content-Type must be multipart/form-data or application/x-www-form-urlencoded",
            status_code=415,
        )

    async with request.form(max_files=1, max_fields=3, max_part_size=max_file_bytes + 1) as form:
        unexpected = sorted(set(form.keys()) - _ALLOWED_FIELDS)
        if unexpected:
            raise UploadParseError(
                "validation_error",
                "request contains unsupported form fields",
                details={"fields": unexpected},
            )
        source_type = _text_field(form, "type")
        if source_type not in {"file", "str"}:
            raise UploadParseError("validation_error", "type must be file or str")
        typed_source = cast(Literal["file", "str"], source_type)
        if authorize is not None:
            await authorize(typed_source)
        try:
            if typed_source == "file":
                source, size_bytes = await _read_file_source(form, max_file_bytes=max_file_bytes)
                return ParsedResourceUpload(
                    source_type="file",
                    source=source,
                    audit={
                        "source_type": "file",
                        "file_name": source.file_name,
                        "mime_type": source.mime_type,
                        "size_bytes": size_bytes,
                    },
                )
            if typed_source == "str":
                unused_file = form.get("file")
                if isinstance(unused_file, UploadFile) or unused_file not in (None, ""):
                    raise UploadParseError(
                        "validation_error",
                        "field 'file' is not allowed when type=str",
                    )
                source = StringSource(content=_text_field(form, "content", preserve=True) or "")
                return ParsedResourceUpload(
                    source_type="str",
                    source=source,
                    audit={"source_type": "str", "size_bytes": len(source.content.encode("utf-8"))},
                )
        except ValidationError as exc:
            raise UploadParseError("validation_error", "resource source validation failed") from exc
        raise AssertionError("validated source type was not handled")  # pragma: no cover


async def _read_file_source(form: FormData, *, max_file_bytes: int) -> tuple[FileSource, int]:
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise UploadParseError("validation_error", "file is required when type=file")
    content_field = _text_field(form, "content", preserve=True)
    if content_field not in (None, ""):
        raise UploadParseError("validation_error", "field 'content' is not allowed when type=file")

    content = bytearray()
    while chunk := await upload.read(_UPLOAD_READ_CHUNK_BYTES):
        content.extend(chunk)
        if len(content) > max_file_bytes:
            raise UploadParseError(
                "payload_too_large",
                "file exceeds the configured limit",
                status_code=413,
            )
    raw = bytes(content)
    try:
        source = FileSource(
            file_name=upload.filename or "",
            mime_type=upload.content_type or "application/octet-stream",
            content_base64=base64.b64encode(raw).decode("ascii"),
        )
    except ValidationError as exc:
        raise UploadParseError("validation_error", "uploaded file metadata is invalid") from exc
    return source, len(raw)


__all__ = ["ParsedResourceUpload", "UploadParseError", "parse_resource_upload"]
