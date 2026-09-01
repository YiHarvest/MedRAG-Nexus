"""验证稳定标识符、内容哈希和核心数据模型。"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from medrag_nexus.core.ids import (
    canonical_text,
    chunk_id,
    content_hash,
    file_id,
    new_id,
    new_task_id,
    normalize_workspace_name,
    text_content_hash,
)
from medrag_nexus.core.models import (
    AddRequest,
    DeleteFileRequest,
    DomainError,
    FileSource,
    TaskRecord,
    TaskResponse,
)
from medrag_nexus.services.files import FileService, _decode_file

WORKSPACE_ID = "workspace_11111111-1111-5111-8111-111111111111"


def test_workspace_name_normalization_preserves_case() -> None:
    assert normalize_workspace_name("  Product   Knowledge ") == "Product Knowledge"
    assert normalize_workspace_name("Product Knowledge") != normalize_workspace_name("product knowledge")


def test_content_hash_text_normalization_and_ids() -> None:
    assert content_hash(b"hello") == "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e"
    assert canonical_text(" Hello\n\tWorld ") == "Hello World"
    assert text_content_hash("Hello  World") == text_content_hash("Hello\nWorld")
    assert text_content_hash("Hello World") != text_content_hash("hello world")
    assert file_id().startswith("file_")

    document_id = new_id()
    assert chunk_id(document_id, 0, "hello") == chunk_id(document_id, 0, "hello")
    assert chunk_id(document_id, 0, "hello") != chunk_id(document_id, 1, "hello")


def test_add_source_is_discriminated_and_preserves_string() -> None:
    file_request = AddRequest.model_validate(
        {
            "user_id": "u1",
            "workspace_id": WORKSPACE_ID,
            "workspace_name": "Knowledge",
            "source": {
                "type": "file",
                "file_name": "a.pdf",
                "mime_type": "application/pdf",
                "content_base64": base64.b64encode(b"%PDF-1.7").decode(),
            },
        }
    )
    assert file_request.source.type == "file"
    string_request = AddRequest.model_validate(
        {
            "user_id": "u1",
            "workspace_id": WORKSPACE_ID,
            "workspace_name": "Knowledge",
            "source": {"type": "str", "content": "  Exact\ntext  "},
        }
    )
    assert string_request.source.content == "  Exact\ntext  "
    with pytest.raises(ValidationError):
        AddRequest.model_validate(
            {
                "user_id": "u1",
                "workspace_id": WORKSPACE_ID,
                "workspace_name": "Knowledge",
                "source": {"type": "str", "content": "   "},
            }
        )


def test_backend_accepts_frontend_provided_workspace_id_without_deriving_it() -> None:
    provided = "workspace_1"
    request = AddRequest.model_validate(
        {
            "user_id": "user-001",
            "workspace_id": provided,
            "workspace_name": "产品知识库",
            "source": {"type": "str", "content": "knowledge"},
        }
    )
    assert FileService._workspace_identity(request) == (provided, "产品知识库")


def test_delete_requires_prefixed_ids() -> None:
    request = DeleteFileRequest(
        user_id="u1",
        workspace_id=WORKSPACE_ID,
        file_id=file_id(),
        file_name="a.pdf",
    )
    assert request.file_id.startswith("file_")
    with pytest.raises(ValidationError):
        DeleteFileRequest(user_id="u1", workspace_id="bad", file_id="bad", file_name="a.pdf")


def test_file_mime_must_match_extension() -> None:
    source = FileSource(
        file_name="report.pdf",
        mime_type="text/plain",
        content_base64=base64.b64encode(b"%PDF-1.7").decode(),
    )
    with pytest.raises(DomainError, match="MIME type does not match"):
        _decode_file(source, 1024)


def test_wps_docx_mime_is_supported() -> None:
    source = FileSource(
        file_name="report.docx",
        mime_type="application/wps-office.docx",
        content_base64=base64.b64encode(b"PK\x03\x04docx-placeholder").decode(),
    )
    file_name, content = _decode_file(source, 1024)
    assert file_name == "report.docx"
    assert content.startswith(b"PK")


def test_task_defaults_and_public_response_omits_internal_operation() -> None:
    task = TaskRecord(
        task_id=new_task_id(),
        user_id="u1",
        workspace_id=WORKSPACE_ID,
        workspace_name="Knowledge",
        operation="add_str",
    )
    public = task.model_dump(exclude={"operation", "payload", "journal", "user_id", "workspace_id", "workspace_name"})
    response = TaskResponse.model_validate(public)
    assert "operation" not in response.model_dump()
    assert response.progress.percent == 0
