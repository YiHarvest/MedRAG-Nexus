"""规范化 Markdown，并将其切分为带来源偏移的检索块。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_PAGE_MARKERS = re.compile(r"--\s*\d+\s+of\s+\d+\s*--", re.IGNORECASE)
_MULTI_SPACES = re.compile(r"[ \t]{2,}")
_MULTI_NEWLINES = re.compile(r"\n{3,}")
_TOKEN_PATTERN = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")
_BOUNDARY_PATTERN = re.compile(r"[\s。！？!?；;，,、：:]")
_BLOCK_PATTERN = re.compile(r"\S(?:.*?\S)?(?=\n{2,}|\Z)", re.DOTALL)


def estimate_tokens(text: str) -> int:
    count = 0
    for match in _TOKEN_PATTERN.finditer(text):
        token = match.group(0)
        if token.isascii() and token.replace("_", "").isalnum():
            count += max(1, (len(token) + 3) // 4)
        else:
            count += 1
    return count


def normalize_markdown(text: str) -> str:
    value = text.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("　", " ").replace("\xa0", " ")
    value = _CONTROL_CHARS.sub("", value)
    value = _PAGE_MARKERS.sub("\n", value)
    lines: list[str] = []
    previous = ""
    for raw in value.splitlines():
        line = _MULTI_SPACES.sub(" ", raw.rstrip()).strip()
        if line and line == previous:
            continue
        lines.append(line)
        previous = line if line else ""
    return _MULTI_NEWLINES.sub("\n\n", "\n".join(lines)).strip()


@dataclass(slots=True)
class ChunkSpan:
    ordinal: int
    content: str
    section: str
    chunk_type: str
    start_offset: int
    end_offset: int
    page_number: int | None


def _chunk_type(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("```"):
        return "code"
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and all("|" in line for line in lines[: min(3, len(lines))]):
        return "table"
    if lines and sum(bool(re.match(r"^\s*(?:[-*+] |\d+[.)] )", line)) for line in lines) >= len(lines) / 2:
        return "list"
    return "paragraph"


def _page_number(markdown: str, position: int) -> int | None:
    matches = list(re.finditer(r"<!--\s*page:\s*(\d+)\s*-->", markdown[:position], re.IGNORECASE))
    return int(matches[-1].group(1)) if matches else None


def _split_span(markdown: str, start: int, end: int, budget: int, overlap: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        remaining = markdown[cursor:end]
        if estimate_tokens(remaining) <= budget:
            spans.append((cursor, end))
            break
        low, high, best = 1, len(remaining), 1
        while low <= high:
            middle = (low + high) // 2
            if estimate_tokens(remaining[:middle]) <= budget:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        prefix = remaining[:best]
        boundaries = [m.end() for m in _BOUNDARY_PATTERN.finditer(prefix)]
        cut = boundaries[-1] if boundaries and boundaries[-1] >= best // 2 else best
        next_end = cursor + cut
        spans.append((cursor, next_end))
        if overlap <= 0:
            cursor = next_end
            continue
        back = next_end
        while back > cursor and estimate_tokens(markdown[back:next_end]) < overlap:
            back -= 1
        cursor = max(cursor + 1, back)
    return spans


def chunk_markdown(markdown: str, *, chunk_size: int, chunk_overlap: int) -> list[ChunkSpan]:
    if not markdown.strip():
        return []
    blocks = list(_BLOCK_PATTERN.finditer(markdown))
    section = "Document"
    raw_spans: list[tuple[int, int, str]] = []
    buffer_start: int | None = None
    buffer_end: int | None = None
    buffer_section = section

    def flush() -> None:
        nonlocal buffer_start, buffer_end
        if buffer_start is not None and buffer_end is not None:
            split_spans = _split_span(markdown, buffer_start, buffer_end, chunk_size, chunk_overlap)
            raw_spans.extend((s, e, buffer_section) for s, e in split_spans)
        buffer_start = buffer_end = None

    for block in blocks:
        text = block.group(0).strip()
        heading = re.match(r"^#{1,6}\s+(.+)$", text)
        if heading and "\n" not in text:
            flush()
            section = heading.group(1).strip()[:200]
            continue
        if buffer_start is None:
            buffer_start, buffer_end, buffer_section = block.start(), block.end(), section
            continue
        candidate = markdown[buffer_start : block.end()]
        if section == buffer_section and estimate_tokens(candidate) <= chunk_size:
            buffer_end = block.end()
        else:
            flush()
            buffer_start, buffer_end, buffer_section = block.start(), block.end(), section
    flush()

    chunks: list[ChunkSpan] = []
    for ordinal, (start, end, current_section) in enumerate(raw_spans):
        content = markdown[start:end].strip()
        if not content:
            continue
        actual_start = markdown.find(content, start, end)
        actual_end = actual_start + len(content)
        chunks.append(
            ChunkSpan(
                ordinal=ordinal,
                content=content,
                section=current_section,
                chunk_type=_chunk_type(content),
                start_offset=actual_start,
                end_offset=actual_end,
                page_number=_page_number(markdown, actual_start),
            )
        )
    return chunks
