"""文档解析、Markdown 分块、检索融合与模型客户端公共入口。"""

from .fusion import SearchCandidate, reciprocal_rank_fusion
from .markdown import ChunkSpan, chunk_markdown, estimate_tokens, normalize_markdown
from .models import EmbeddingClient, RerankClient
from .parsers import (
    SUPPORTED_EXTENSIONS,
    ParseResult,
    extract_mineru_markdown,
    parse_file,
    safe_file_name,
    sniff_extension,
    validate_file_type,
)

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "ChunkSpan",
    "EmbeddingClient",
    "ParseResult",
    "RerankClient",
    "SearchCandidate",
    "chunk_markdown",
    "estimate_tokens",
    "extract_mineru_markdown",
    "normalize_markdown",
    "parse_file",
    "reciprocal_rank_fusion",
    "safe_file_name",
    "sniff_extension",
    "validate_file_type",
]
