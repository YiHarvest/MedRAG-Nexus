"""公共 HTTP 路由共用的 OpenAPI 元数据。"""

from __future__ import annotations

from typing import Any

from medrag_nexus.core.models import ErrorResponse, HealthResponse

OPENAPI_TAGS = [
    {"name": "知识新增", "description": "异步新增 PDF、TXT、DOCX 文件或 workspace 字符串知识。"},
    {"name": "列表", "description": "同步列出用户的 workspace 或 workspace 下的文件。"},
    {"name": "删除", "description": "按永久 file_id 异步删除文件。"},
    {"name": "检索", "description": "同步执行 workspace 向量、BM25、RRF 与 Rerank 混合检索。"},
    {"name": "聊天", "description": "调用只读知识工具并流式生成回答。"},
    {"name": "任务", "description": "查询新增或删除接口创建的异步任务。"},
    {"name": "健康检查", "description": "检查 API 进程和外部依赖。"},
]


def documented_error(description: str, code: str, message: str) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": code,
                        "message": message,
                        "request_id": "8b6e50fa-e93a-4c0b-a86d-a87178cb27bf",
                    }
                }
            }
        },
    }


VALIDATION_ERROR = documented_error("请求字段不合法。", "validation_error", "request validation failed")
INTERNAL_ERROR = documented_error("服务器内部错误。", "internal_error", "an unexpected internal error occurred")
QUEUE_ERROR = documented_error("异步任务后端不可用。", "redis_unavailable", "Redis is unavailable")
HEALTH_UNAVAILABLE = {
    "model": HealthResponse,
    "description": "一个或多个核心依赖不可用。",
}

ADD_REQUEST_BODY = {
    "required": True,
    "description": (
        "异步新增知识：提交 PDF、TXT、DOCX 文件或普通字符串。任务创建成功返回 202，"
        "实际入库处理异步执行，可通过任务接口查询处理结果。各字段说明见下方参数。"
    ),
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["user_id", "workspace_id", "workspace_name", "type"],
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "用户业务标识，必填，由前端提供。",
                        "default": "",
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": "知识空间 ID，必填，由前端提供；后端不根据其他字段生成。",
                        "default": "",
                    },
                    "workspace_name": {
                        "type": "string",
                        "description": "知识空间名称，必填。",
                        "default": "",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["file", "str"],
                        "description": "提交类型，必填。file 上传文件；str 提交普通字符串。",
                    },
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "type=file 时必填；仅支持 PDF、TXT、DOCX。",
                    },
                    "content": {
                        "type": "string",
                        "description": "type=str 时必填，要入库的原文内容。",
                        "default": "",
                    },
                    "callback_url": {
                        "type": "string",
                        "format": "uri",
                        "description": "可选。任务状态变化时接收包含 task_id 的 HTTP POST 回调。",
                        "default": "",
                    },
                },
            }
        }
    },
}


__all__ = [
    "ADD_REQUEST_BODY",
    "HEALTH_UNAVAILABLE",
    "INTERNAL_ERROR",
    "OPENAPI_TAGS",
    "QUEUE_ERROR",
    "VALIDATION_ERROR",
    "documented_error",
]
