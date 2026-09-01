"""FastAPI 应用工厂的公共入口。"""

from .application import create_app


def run() -> None:
    """启动 HTTP API、内嵌 Worker 与 MCP 服务。"""
    from .main import run as start

    start()


__all__ = ["create_app", "run"]
