"""生成并规范化知识库使用的稳定标识符与内容哈希。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from uuid import UUID, uuid4, uuid5


def new_id() -> UUID:
    return uuid4()


def new_task_id() -> str:
    return uuid4().hex


def normalize_workspace_name(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip())


def file_id() -> str:
    return f"file_{uuid4()}"


def canonical_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip())


def content_hash(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()[:32]}"


def text_content_hash(content: str) -> str:
    return content_hash(canonical_text(content).encode("utf-8"))


def chunk_id(document_id: UUID, ordinal: int, content: str) -> UUID:
    digest = content_hash(content.encode("utf-8"))
    return uuid5(document_id, f"{ordinal}:{digest}")
