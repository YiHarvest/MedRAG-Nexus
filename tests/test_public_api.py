"""验证包公共导出、MCP 工具和存储命名约束。"""

from __future__ import annotations

import pytest

from jd_knowledge import __version__
from jd_knowledge.api import create_app
from jd_knowledge.core import AddRequest, Settings
from jd_knowledge.mcp import mcp
from jd_knowledge.pipeline import parse_file
from jd_knowledge.services import FileService, Runtime
from jd_knowledge.storage import ArtifactStore, SQLiteStore


def test_package_initializers_export_public_api() -> None:
    assert __version__ == "0.3.0"
    assert all(
        value is not None
        for value in (
            create_app,
            AddRequest,
            Settings,
            parse_file,
            FileService,
            Runtime,
            ArtifactStore,
            SQLiteStore,
        )
    )


async def test_mcp_exposes_one_merged_add_tool() -> None:
    names = [tool.name for tool in await mcp.list_tools()]
    assert names == ["add", "list_workspaces", "list_files", "delete_file", "get_task", "retrieve"]
    assert "add_file" not in names
    assert "add_str" not in names


def test_active_storage_names_cannot_be_legacy_cleanup_targets() -> None:
    with pytest.raises(ValueError, match="must differ"):
        Settings(
            _env_file=None,
            openai_embedding_url="http://embedding.test/v1/embeddings",
            milvus_collection="same_collection",
            legacy_milvus_collection="same_collection",
        )
