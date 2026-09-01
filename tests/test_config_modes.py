from pathlib import Path

import pytest

from medrag_nexus.core.config import Settings


@pytest.mark.parametrize(
    ("mode", "database_name", "search_prefix", "vector_prefix"),
    [
        ("dev", "MedRAG-Nexus-dev", "medrag-nexus-dev", "medrag_nexus_dev"),
        ("prd", "MedRAG-Nexus-prd", "medrag-nexus-prd", "medrag_nexus_prd"),
    ],
)
def test_app_mode_selects_isolated_storage_names(
    mode: str,
    database_name: str,
    search_prefix: str,
    vector_prefix: str,
) -> None:
    settings = Settings(
        app_mode=mode,
        openai_embedding_url="http://embedding.test/v1/embeddings",
        _env_file=None,
    )

    assert settings.database_name == database_name
    assert settings.data_root == (Path("./data") / database_name).resolve()
    assert settings.sqlite_path == settings.data_root / f"{database_name}.sqlite3"
    assert settings.elasticsearch_workspace_index == f"{search_prefix}-workspaces"
    assert settings.elasticsearch_document_index == f"{search_prefix}-resources"
    assert settings.elasticsearch_chunk_index == f"{search_prefix}-chunks"
    assert settings.milvus_collection == f"{vector_prefix}_chunks"
    assert settings.redis_queue_name == f"medrag-nexus:{mode}:tasks"

    assert settings.webui_data_root == settings.data_root / "webui"
    assert settings.webui_sqlite_path == settings.webui_data_root / f"{database_name}-webui.sqlite3"
    assert settings.webui_elasticsearch_workspace_index == f"{search_prefix}-webui-workspaces"
    assert settings.webui_elasticsearch_document_index == f"{search_prefix}-webui-resources"
    assert settings.webui_elasticsearch_chunk_index == f"{search_prefix}-webui-chunks"
    assert settings.webui_milvus_collection == f"{vector_prefix}_webui_chunks"
    assert settings.webui_redis_queue_name == f"medrag-nexus:{mode}:webui:tasks"


def test_explicit_storage_overrides_are_preserved(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "custom.sqlite3"
    settings = Settings(
        app_mode="prd",
        openai_embedding_url="http://embedding.test/v1/embeddings",
        sqlite_path=sqlite_path,
        milvus_collection="custom_collection",
        elasticsearch_chunk_index="custom-chunks",
        _env_file=None,
    )

    assert settings.sqlite_path == sqlite_path.resolve()
    assert settings.milvus_collection == "custom_collection"
    assert settings.elasticsearch_chunk_index == "custom-chunks"
