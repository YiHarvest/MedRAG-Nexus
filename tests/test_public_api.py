"""验证包公共导出、MCP 工具和存储命名约束。"""

from __future__ import annotations

import pytest

from medrag_nexus import __version__
from medrag_nexus.api import create_app
from medrag_nexus.core import AddRequest, Settings
from medrag_nexus.mcp import mcp
from medrag_nexus.pipeline import parse_file
from medrag_nexus.services import FileService, Runtime
from medrag_nexus.storage import ArtifactStore, SQLiteStore


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
