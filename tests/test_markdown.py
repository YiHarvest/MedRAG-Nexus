"""验证 Markdown 规范化、分块和偏移量计算。"""

from __future__ import annotations

from medrag_nexus.pipeline.markdown import chunk_markdown, estimate_tokens, normalize_markdown


def test_normalize_markdown_keeps_structure_and_removes_noise() -> None:
    value = normalize_markdown("# 标题\r\n\r\n重复\n重复\n\n\n正文\x00")
    assert value == "# 标题\n\n重复\n\n正文"


def test_chunk_markdown_tracks_offsets_and_budget() -> None:
    markdown = "# 第一章\n\n" + "这是一段测试文本。" * 80 + "\n\n## 第二章\n\n短内容。"
    chunks = chunk_markdown(markdown, chunk_size=40, chunk_overlap=8)
    assert len(chunks) > 2
    assert all(chunk.content == markdown[chunk.start_offset : chunk.end_offset] for chunk in chunks)
    assert all(estimate_tokens(chunk.content) <= 40 for chunk in chunks)
    assert chunks[-1].section == "第二章"
