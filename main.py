"""MedRAG-Nexus 服务的命令行启动入口。"""

from __future__ import annotations

import argparse

import uvicorn

from medrag_nexus.core.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Start MedRAG-Nexus API, Redis worker, and FastMCP HTTP service.")
    parser.add_argument("--reload", action="store_true", help="Reload the development server when Python files change.")
    args = parser.parse_args()
    settings = get_settings()
    uvicorn.run(
        "medrag_nexus.api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.app_log_level,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
