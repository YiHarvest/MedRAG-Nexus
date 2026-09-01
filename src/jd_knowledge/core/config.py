"""
配置模块 - 管理应用程序的所有配置项

该模块使用 pydantic-settings 从环境变量和 .env 文件加载配置，
提供类型安全的配置访问和验证功能。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    应用程序配置类

    从环境变量和 .env 文件加载配置，提供类型安全的配置访问。
    所有配置项都有合理的默认值，可以通过环境变量覆盖。
    """

    # 模型配置：指定从 .env 文件读取配置，使用 UTF-8 编码
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ========== 应用基础配置 ==========
    app_host: str = "0.0.0.0"  # 应用监听地址
    app_port: int = 28111  # 应用监听端口
    app_log_level: str = "info"  # 日志级别
    data_root: Path = Path("./data")  # 公开 API/MCP 数据存储根目录

    # ========== 自有 WebUI 账号（不影响公开 API/MCP） ==========
    webui_superadmin_username: str = ""
    webui_superadmin_password: str = ""
    webui_superadmin_display_name: str = "超级管理员"
    webui_superadmins_json: str = ""
    webui_cookie_secure: bool = False
    webui_lock_password: str = ""
    webui_cleanup_retry_seconds: int = 60
    webui_deletion_lease_seconds: int = 300
    webui_agent_action_ttl_minutes: int = 15
    webui_agent_action_retention_days: int = 30
    webui_agent_artifact_ttl_hours: int = 24
    webui_agent_cleanup_interval_seconds: int = 3600
    webui_trust_proxy_headers: bool = False
    webui_trusted_proxy_hops: int = 1

    # WebUI 的知识数据与公开 API/MCP 完全隔离。连接地址留空时复用同一服务端，
    # 但 SQLite 文件、ES 索引、Milvus 集合、Redis 队列和文件目录始终独立。
    webui_data_root: Path = Path("./data/webui")
    webui_sqlite_path: Path = Path("./data/webui/jd_knowledge_webui.sqlite3")
    webui_elasticsearch_url: str = ""
    webui_elasticsearch_username: str = ""
    webui_elasticsearch_password: str = ""
    webui_elasticsearch_api_key: str = ""
    webui_elasticsearch_workspace_index: str = "jd_knowledge_webui_workspaces"
    webui_elasticsearch_document_index: str = "jd_knowledge_webui_resources"
    webui_elasticsearch_chunk_index: str = "jd_knowledge_webui_chunks"
    webui_milvus_host: str = ""
    webui_milvus_port: int = 0
    webui_milvus_token: str = ""
    webui_milvus_collection: str = "jd_knowledge_webui_chunks"
    webui_redis_url: str = ""
    webui_redis_queue_name: str = "knowledge:webui:tasks"
    webui_audit_log_dir: Path = Path("./data/audit/webui")
    webui_audit_log_retention_months: int = 3

    # ========== MinerU 文档解析服务配置 ==========
    mineru_url: str = ""  # MinerU 服务地址
    mineru_api_path: str = "/file_parse"  # MinerU API 路径
    mineru_backend: str = "pipeline"  # MinerU 后端类型
    mineru_method: str = "auto"  # MinerU 解析方法
    mineru_lang: str = "ch"  # MinerU 解析语言
    mineru_max_concurrency: int = 8  # 单份 PDF 调用远端 VLM 的最大页级并发
    mineru_http_timeout_seconds: int = 1800  # 单次 MinerU HTTP 请求超时
    file_ingestion_concurrency: int = 1  # 大文件解析与入库并发，避免多 PDF 耗尽内存/CPU

    # ========== Milvus 向量数据库配置 ==========
    milvus_host: str = "localhost"  # Milvus 服务地址
    milvus_port: int = 19530  # Milvus 服务端口
    milvus_token: str = ""  # Milvus 认证令牌
    milvus_collection: str = "jd_knowledge_v3_chunks"  # 公开 API/MCP Milvus 集合

    # ========== Elasticsearch 搜索引擎配置 ==========
    elasticsearch_url: str = "http://localhost:9200"  # Elasticsearch 服务地址
    elasticsearch_username: str = "elastic"  # Elasticsearch 用户名
    elasticsearch_password: str = ""  # Elasticsearch 密码
    elasticsearch_api_key: str = ""  # Elasticsearch API 密钥
    # Elasticsearch 索引名称配置
    elasticsearch_workspace_index: str = "jd_knowledge_v3_workspaces"  # Workspace 冗余索引
    elasticsearch_document_index: str = "jd_knowledge_v3_resources"  # 资源冗余索引
    elasticsearch_chunk_index: str = "jd_knowledge_v3_chunks"  # 文本块索引
    legacy_elasticsearch_indices: str = (
        "jd_knowledge_v2_workspaces,jd_knowledge_v2_documents,jd_knowledge_v2_chunks,jd_knowledge_v2_tasks"
    )

    # ========== Redis 任务队列配置 ==========
    redis_url: str = "redis://127.0.0.1:20000/0"  # 固定 IPv4，避免 localhost 优先解析到未监听的 ::1
    worker_concurrency: int = 4
    redis_queue_name: str = "knowledge:tasks"
    task_timeout_seconds: int = 3600
    workspace_lock_ttl_seconds: int = 60
    workspace_lock_wait_seconds: int = 300
    reservation_ttl_seconds: int = 3900
    stage_retry_count: int = 3

    sqlite_path: Path = Path("./data/agenthub.sqlite3")
    migration_marker: str = "v3_legacy_cleanup_complete"
    legacy_milvus_collection: str = "jd_knowledge_v2_chunks"

    # ========== OpenAI Embedding 向量化服务配置 ==========
    openai_embedding_url: str  # OpenAI Embedding 服务地址（必填）
    openai_embedding_api_key: str = ""  # OpenAI Embedding API 密钥
    embedding_model: str = "bge-m3"  # 向量化模型名称
    embedding_dimension: int = 1024  # 向量维度
    embedding_batch_size: int = 64  # 向量化批处理大小

    # ========== Rerank 重排序服务配置 ==========
    rerank_url: str = ""  # Rerank 服务地址
    rerank_api_key: str = ""  # Rerank API 密钥
    rerank_model: str = "bge-reranker-v2-m3"  # 重排序模型名称

    # ========== OpenAI 兼容聊天模型配置 ==========
    llm_url: str = ""  # OpenAI 兼容 API Base URL
    llm_model: str = ""  # 聊天模型名称
    llm_key: str = ""  # 聊天模型 API 密钥
    llm_thinking_enabled: bool = False  # 是否启用支持模型的思考模式
    llm_timeout_seconds: float = 120.0
    chat_router_model: str = ""  # 前置意图分类模型；为空时复用主聊天模型
    chat_router_timeout_seconds: float = 15.0
    chat_router_max_tokens: int = 96
    chat_workspace_concurrency: int = 4
    chat_max_tool_calls: int = 4

    # ========== 文本分块配置 ==========
    chunk_size: int = 512  # 文本块大小（字符数）
    chunk_overlap: int = 120  # 文本块重叠大小（字符数）

    # ========== 检索配置 ==========
    retrieval_default_top_k: int = 10  # 默认返回的文档数量
    retrieval_max_top_k: int = 50  # 最大返回的文档数量
    retrieval_candidate_multiplier: int = 5  # 候选文档倍数
    retrieval_min_candidates: int = 50  # 最小候选文档数
    retrieval_max_candidates: int = 200  # 最大候选文档数
    rrf_k: int = 60  # RRF（Reciprocal Rank Fusion）参数 K
    rerank_max_candidates: int = 200  # 重排序最大候选文档数

    # ========== 文件和任务限制配置 ==========
    max_file_size_mib: int = 50  # 最大文件大小（MiB）
    max_text_size_mib: int = 10  # 最大文本大小（MiB）
    task_retention_days: int = 30  # 任务保留天数
    task_log_retention_days: int = 7  # 任务日志保留天数（超过自动清除）
    dependency_timeout_seconds: float = 3.0  # 依赖服务超时时间（秒）

    @field_validator("data_root", "webui_data_root", "webui_audit_log_dir", mode="after")
    @classmethod
    def normalize_data_root(cls, value: Path) -> Path:
        """
        规范化数据根目录路径

        将路径展开（处理 ~ 符号）并转换为绝对路径

        Args:
            value: 原始路径

        Returns:
            规范化后的绝对路径
        """
        return value.expanduser().resolve()

    @field_validator("sqlite_path", "webui_sqlite_path", mode="after")
    @classmethod
    def normalize_sqlite_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def validate_ranges(self) -> Settings:
        """
        验证配置项的范围和一致性

        检查以下约束：
        - chunk_overlap 必须在 0 到 chunk_size 之间
        - embedding_dimension 必须为 1024（bge-m3 模型要求）
        - retrieval_default_top_k 必须在 1 到 retrieval_max_top_k 之间
        - worker_concurrency 必须至少为 1

        Returns:
            验证通过的 Settings 实例

        Raises:
            ValueError: 当配置项不符合约束时
        """
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must satisfy 0 <= overlap < CHUNK_SIZE")
        if self.embedding_dimension != 1024:
            raise ValueError("EMBEDDING_DIMENSION must be 1024 for the configured bge-m3 contract")
        if not 1 <= self.retrieval_default_top_k <= self.retrieval_max_top_k:
            raise ValueError("retrieval top_k defaults are inconsistent")
        if self.worker_concurrency < 1:
            raise ValueError("WORKER_CONCURRENCY must be at least 1")
        if self.file_ingestion_concurrency < 1:
            raise ValueError("FILE_INGESTION_CONCURRENCY must be at least 1")
        if self.mineru_max_concurrency < 1:
            raise ValueError("MINERU_MAX_CONCURRENCY must be at least 1")
        if self.mineru_http_timeout_seconds < 60:
            raise ValueError("MINERU_HTTP_TIMEOUT_SECONDS must be at least 60")
        if self.llm_timeout_seconds < 10:
            raise ValueError("LLM_TIMEOUT_SECONDS must be at least 10")
        if self.chat_router_timeout_seconds < 1:
            raise ValueError("CHAT_ROUTER_TIMEOUT_SECONDS must be at least 1")
        if not 32 <= self.chat_router_max_tokens <= 256:
            raise ValueError("CHAT_ROUTER_MAX_TOKENS must be between 32 and 256")
        if self.chat_workspace_concurrency < 1:
            raise ValueError("CHAT_WORKSPACE_CONCURRENCY must be at least 1")
        if not 1 <= self.chat_max_tool_calls <= 5:
            raise ValueError("CHAT_MAX_TOOL_CALLS must be between 1 and 5")
        if self.webui_cleanup_retry_seconds < 5:
            raise ValueError("WEBUI_CLEANUP_RETRY_SECONDS must be at least 5")
        if self.webui_deletion_lease_seconds < 5:
            raise ValueError("WEBUI_DELETION_LEASE_SECONDS must be at least 5")
        if self.webui_agent_action_ttl_minutes < 1:
            raise ValueError("WEBUI_AGENT_ACTION_TTL_MINUTES must be at least 1")
        if self.webui_agent_action_retention_days < 1:
            raise ValueError("WEBUI_AGENT_ACTION_RETENTION_DAYS must be at least 1")
        if self.webui_agent_artifact_ttl_hours < 1:
            raise ValueError("WEBUI_AGENT_ARTIFACT_TTL_HOURS must be at least 1")
        if self.webui_agent_cleanup_interval_seconds < 60:
            raise ValueError("WEBUI_AGENT_CLEANUP_INTERVAL_SECONDS must be at least 60")
        if self.webui_trusted_proxy_hops < 1:
            raise ValueError("WEBUI_TRUSTED_PROXY_HOPS must be at least 1")
        if self.webui_audit_log_retention_months < 1:
            raise ValueError("WEBUI_AUDIT_LOG_RETENTION_MONTHS must be at least 1")
        if self.workspace_lock_ttl_seconds < 10:
            raise ValueError("WORKSPACE_LOCK_TTL_SECONDS must be at least 10")
        if self.stage_retry_count != 3:
            raise ValueError("STAGE_RETRY_COUNT must be 3")
        if self.milvus_collection == self.legacy_milvus_collection:
            raise ValueError("MILVUS_COLLECTION must differ from LEGACY_MILVUS_COLLECTION")
        active_indices = {
            self.elasticsearch_workspace_index,
            self.elasticsearch_document_index,
            self.elasticsearch_chunk_index,
        }
        overlap = active_indices.intersection(self.legacy_elasticsearch_index_names)
        if overlap:
            raise ValueError(f"active Elasticsearch indices cannot be legacy cleanup targets: {sorted(overlap)}")
        if self.sqlite_path == self.webui_sqlite_path:
            raise ValueError("WEBUI_SQLITE_PATH must differ from SQLITE_PATH")
        if self.milvus_collection == self.webui_milvus_collection:
            raise ValueError("WEBUI_MILVUS_COLLECTION must differ from MILVUS_COLLECTION")
        public_indices = {
            self.elasticsearch_workspace_index,
            self.elasticsearch_document_index,
            self.elasticsearch_chunk_index,
        }
        webui_indices = {
            self.webui_elasticsearch_workspace_index,
            self.webui_elasticsearch_document_index,
            self.webui_elasticsearch_chunk_index,
        }
        if public_indices.intersection(webui_indices):
            raise ValueError("WebUI Elasticsearch indices must differ from public API/MCP indices")
        return self

    def webui_runtime_settings(self) -> Settings:
        """生成 WebUI 独立知识存储使用的 Runtime 配置。"""

        return self.model_copy(
            update={
                "data_root": self.webui_data_root,
                "sqlite_path": self.webui_sqlite_path,
                "elasticsearch_url": self.webui_elasticsearch_url or self.elasticsearch_url,
                "elasticsearch_username": (
                    self.webui_elasticsearch_username or self.elasticsearch_username
                ),
                "elasticsearch_password": (
                    self.webui_elasticsearch_password or self.elasticsearch_password
                ),
                "elasticsearch_api_key": self.webui_elasticsearch_api_key or self.elasticsearch_api_key,
                "elasticsearch_workspace_index": self.webui_elasticsearch_workspace_index,
                "elasticsearch_document_index": self.webui_elasticsearch_document_index,
                "elasticsearch_chunk_index": self.webui_elasticsearch_chunk_index,
                "milvus_host": self.webui_milvus_host or self.milvus_host,
                "milvus_port": self.webui_milvus_port or self.milvus_port,
                "milvus_token": self.webui_milvus_token or self.milvus_token,
                "milvus_collection": self.webui_milvus_collection,
                "redis_url": self.webui_redis_url or self.redis_url,
                "redis_queue_name": self.webui_redis_queue_name,
                "migration_marker": "webui_storage_initialized",
                "legacy_elasticsearch_indices": "",
                "legacy_milvus_collection": "__webui_no_legacy_collection__",
            }
        )

    @property
    def legacy_elasticsearch_index_names(self) -> list[str]:
        return [value.strip() for value in self.legacy_elasticsearch_indices.split(",") if value.strip()]

    @property
    def max_file_bytes(self) -> int:
        """最大文件大小（字节）"""
        return self.max_file_size_mib * 1024 * 1024

    @property
    def max_text_bytes(self) -> int:
        """最大文本大小（字节）"""
        return self.max_text_size_mib * 1024 * 1024

    @property
    def log_root(self) -> Path:
        """任务日志根目录（data/log）"""
        return self.data_root / "log"

    @property
    def mineru_uses_openai_server(self) -> bool:
        """检查 MinerU 是否使用 OpenAI 服务器"""
        return self.mineru_backend.endswith("http-client")

    def candidate_k(self, top_k: int) -> int:
        """
        计算候选文档数量

        根据请求的 top_k 值，计算需要检索的候选文档数量。
        该值用于混合检索策略，确保有足够的候选文档进行重排序。

        Args:
            top_k: 用户请求的返回文档数量

        Returns:
            计算得出的候选文档数量
        """
        return min(
            max(top_k * self.retrieval_candidate_multiplier, self.retrieval_min_candidates),
            self.retrieval_max_candidates,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    获取配置实例（单例模式）

    使用 LRU 缓存确保配置只加载一次，提高性能。

    Returns:
        Settings 配置实例
    """
    return Settings()  # type: ignore[call-arg]
