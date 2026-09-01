"""验证文件类型探测、校验和 MinerU 解析流程。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jd_knowledge.pipeline.parsers import (
    _mineru_file_api,
    _mineru_http_client,
    _read_mineru_markdown,
    _safe_endpoint,
    extract_mineru_markdown,
    parse_file,
    safe_file_name,
    sniff_extension,
    validate_file_type,
)


def test_safe_file_name_removes_directories() -> None:
    assert safe_file_name("../../报告.md") == "报告.md"


def test_text_file_sniff_and_validation(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("# 知识\n\n正文", encoding="utf-8")
    assert sniff_extension(path) == ".txt"
    assert validate_file_type(path) == ".txt"


def test_text_sniff_allows_utf8_character_split_at_sample_boundary(tmp_path) -> None:
    path = tmp_path / "frontend.txt"
    path.write_text("# 前端入库一致性验证\n\n校验口令是蓝鲸七号。", encoding="utf-8")

    assert sniff_extension(path) == ".txt"
    assert validate_file_type(path) == ".txt"


def test_markdown_extension_is_not_supported(tmp_path) -> None:
    path = tmp_path / "note.md"
    path.write_text("# 知识", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported file extension"):
        validate_file_type(path)


def test_extension_mismatch_is_rejected(tmp_path) -> None:
    path = tmp_path / "fake.pdf"
    path.write_text("not a pdf", encoding="utf-8")
    try:
        validate_file_type(path)
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("mismatched extension was accepted")


def test_extracts_current_mineru_response_shape() -> None:
    payload = {"backend": "pipeline", "results": {"paper": {"md_content": "# Parsed"}}}
    assert extract_mineru_markdown(payload) == "# Parsed"


def test_reads_generated_mineru_markdown(tmp_path) -> None:
    output = tmp_path / "document" / "vlm"
    output.mkdir(parents=True)
    (output / "document.md").write_text("# Parsed by remote VLM\n", encoding="utf-8")
    assert _read_mineru_markdown(tmp_path) == "# Parsed by remote VLM"


def test_mineru_log_endpoint_removes_credentials_and_query() -> None:
    endpoint = _safe_endpoint("https://user:secret@mineru.example:8443/v1?token=sensitive#fragment")

    assert endpoint == "https://mineru.example:8443/v1"


async def test_text_parser_reports_detailed_progress(tmp_path, monkeypatch) -> None:
    async def inline_to_thread(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    monkeypatch.setattr("jd_knowledge.pipeline.parsers.asyncio.to_thread", inline_to_thread)
    path = tmp_path / "note.txt"
    path.write_text("# 知识\n\n正文", encoding="utf-8")
    events: list[tuple[str, str, dict[str, object]]] = []

    async def progress(level: str, message: str, context: dict[str, object]) -> None:
        events.append((level, message, context))

    result = await parse_file(path, object(), progress=progress)  # type: ignore[arg-type]

    assert result.parser == "text"
    assert [message for _, message, _ in events] == [
        "文件类型校验完成",
        "开始检测文本编码并提取内容",
        "文本文件解析完成",
    ]
    assert events[-1][2]["characters"] == len(result.markdown)


async def test_mineru_file_api_reports_connection_and_response_details(tmp_path, monkeypatch) -> None:
    async def inline_to_thread(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    class FakeResponse:
        is_success = True
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"results": {"document": {"md_content": "# Parsed"}}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("jd_knowledge.pipeline.parsers.asyncio.to_thread", inline_to_thread)
    monkeypatch.setattr("jd_knowledge.pipeline.parsers.httpx.AsyncClient", lambda **kwargs: FakeClient())
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7\nexample")
    settings = SimpleNamespace(
        mineru_url="https://user:secret@mineru.example:8443",
        mineru_api_path="/file_parse",
        mineru_backend="pipeline",
        mineru_method="auto",
        mineru_lang="ch",
    )
    events: list[tuple[str, str, dict[str, object]]] = []

    async def progress(level: str, message: str, context: dict[str, object]) -> None:
        events.append((level, message, context))

    markdown = await _mineru_file_api(path, settings, progress)

    assert markdown == "# Parsed"
    assert [message for _, message, _ in events] == [
        "准备连接 MinerU 文件解析接口",
        "开始向 MinerU 上传并解析文件",
        "MinerU 接口响应已收到",
        "MinerU Markdown 解析完成",
    ]
    assert events[0][2]["endpoint"] == "https://mineru.example:8443/file_parse"
    assert events[2][2]["status"] == 200


async def test_mineru_http_client_limits_page_concurrency_and_extends_timeout(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_parse(**kwargs) -> None:
        captured.update(kwargs)
        output = tmp_path / "generated"
        output.mkdir()
        (output / "document.md").write_text("# Parsed", encoding="utf-8")
        source_output = kwargs["output_dir"]
        monkeypatch.setattr("jd_knowledge.pipeline.parsers._read_mineru_markdown", lambda path: "# Parsed")
        assert source_output

    monkeypatch.setattr("mineru.cli.common.aio_do_parse", fake_parse)
    monkeypatch.setattr("mineru.cli.common.read_fn", lambda path: b"%PDF-1.7\nexample")
    path = tmp_path / "large.pdf"
    path.write_bytes(b"%PDF-1.7\nexample")
    settings = SimpleNamespace(
        mineru_url="http://mineru.example/v1",
        mineru_backend="vlm-http-client",
        mineru_method="auto",
        mineru_lang="ch",
        mineru_max_concurrency=8,
        mineru_http_timeout_seconds=1800,
    )

    markdown = await _mineru_http_client(path, settings)

    assert markdown == "# Parsed"
    assert captured["max_concurrency"] == 8
    assert captured["http_timeout"] == 1800
