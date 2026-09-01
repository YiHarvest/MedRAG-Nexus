"""解析公共知识新增接口的 multipart 请求。"""

from __future__ import annotations

import base64
import json

from fastapi import Request
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile

from jd_knowledge.core.models import AddRequest, DomainError, FileSource, StringSource

from .http import api_log

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
_UPLOAD_PROGRESS_STEP_BYTES = 8 * 1024 * 1024


def _form_string(form: FormData, key: str, *, preserve: bool = False) -> str | None:
    value = form.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainError("validation_error", f"multipart field '{key}' must be text", status_code=422)
    return value if preserve else (value.strip() or None)


async def parse_add_request(request: Request, *, max_file_bytes: int) -> AddRequest:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "multipart/form-data":
        raise DomainError(
            "unsupported_media_type",
            "Content-Type must be multipart/form-data",
            status_code=415,
        )
    async with request.form(max_files=1, max_fields=7, max_part_size=max_file_bytes + 1) as form:
        source_type = _form_string(form, "type")
        await api_log(
            request,
            "INFO",
            "上传表单解析完成",
            source_type=source_type or "unknown",
            field_count=len(form),
            fields=",".join(sorted(form.keys())),
        )
        allowed = {"user_id", "workspace_id", "workspace_name", "type", "file", "content", "callback_url"}
        unexpected = sorted(set(form.keys()) - allowed)
        if unexpected:
            raise DomainError(
                "validation_error",
                "request contains unsupported multipart fields",
                status_code=422,
                details={"fields": unexpected},
            )
        if source_type == "file":
            source = await _read_file_source(request, form, max_file_bytes=max_file_bytes)
        elif source_type == "str":
            unused_file = form.get("file")
            if isinstance(unused_file, UploadFile) or unused_file not in (None, ""):
                raise DomainError("validation_error", "field 'file' is not allowed when type=str", status_code=422)
            source = StringSource(content=_form_string(form, "content", preserve=True) or "")
            await api_log(request, "INFO", "字符串知识读取完成", size_bytes=len(source.content.encode("utf-8")))
        else:
            raise DomainError("validation_error", "field 'type' must be file or str", status_code=422)
        try:
            return AddRequest(
                user_id=_form_string(form, "user_id"),
                workspace_id=_form_string(form, "workspace_id"),
                workspace_name=_form_string(form, "workspace_name"),
                source=source,
                callback_url=_form_string(form, "callback_url"),
            )
        except ValidationError as exc:
            raise DomainError(
                "validation_error",
                "request validation failed",
                status_code=422,
                details={"errors": json.loads(exc.json(include_url=False))},
            ) from exc


async def _read_file_source(request: Request, form: FormData, *, max_file_bytes: int) -> FileSource:
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise DomainError("validation_error", "field 'file' is required when type=file", status_code=422)
    unused_content = _form_string(form, "content", preserve=True)
    if unused_content not in (None, ""):
        raise DomainError("validation_error", "field 'content' is not allowed when type=file", status_code=422)
    expected_bytes = upload.size if isinstance(upload.size, int) else None
    await api_log(
        request,
        "INFO",
        "开始读取上传文件",
        file_name=upload.filename or "<unnamed>",
        mime_type=upload.content_type or "application/octet-stream",
        expected_bytes=expected_bytes,
        max_bytes=max_file_bytes,
        progress="0%" if expected_bytes else "0 bytes",
    )
    content_buffer = bytearray()
    next_report_bytes = _UPLOAD_PROGRESS_STEP_BYTES
    last_reported_bucket = 0
    while chunk := await upload.read(_UPLOAD_READ_CHUNK_BYTES):
        content_buffer.extend(chunk)
        received_bytes = len(content_buffer)
        if received_bytes > max_file_bytes:
            await api_log(
                request,
                "WARN",
                "上传文件超过大小限制",
                file_name=upload.filename or "<unnamed>",
                received_bytes=received_bytes,
                max_bytes=max_file_bytes,
            )
            raise DomainError("payload_too_large", "file exceeds the configured limit", status_code=413)
        if expected_bytes:
            percent = min(100, round(received_bytes / expected_bytes * 100))
            bucket = percent // 10 * 10
            if bucket > last_reported_bucket or received_bytes == expected_bytes:
                last_reported_bucket = bucket
                await api_log(
                    request,
                    "INFO",
                    "上传文件读取进度",
                    file_name=upload.filename or "<unnamed>",
                    received_bytes=received_bytes,
                    expected_bytes=expected_bytes,
                    progress=f"{percent}%",
                )
        elif received_bytes >= next_report_bytes:
            await api_log(
                request,
                "INFO",
                "上传文件读取进度",
                file_name=upload.filename or "<unnamed>",
                received_bytes=received_bytes,
            )
            next_report_bytes += _UPLOAD_PROGRESS_STEP_BYTES
    content = bytes(content_buffer)
    await api_log(
        request,
        "INFO",
        "上传文件读取完成",
        file_name=upload.filename or "<unnamed>",
        size_bytes=len(content),
        progress="100%",
    )
    return FileSource(
        file_name=upload.filename or "",
        mime_type=upload.content_type or "application/octet-stream",
        content_base64=base64.b64encode(content).decode("ascii"),
    )


__all__ = ["parse_add_request"]
