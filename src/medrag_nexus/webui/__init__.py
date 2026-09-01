"""WebUI 专用的认证与授权旁路模块。

该包与公共 ``/api/v1`` 和 MCP 契约隔离，应用通过构建并挂载其路由启用功能。
"""

from .integration import WebUiFeature
from .permissions import PermissionEngine, PermissionRegistry, build_default_registry
from .router import WebUiPrincipal, create_principal_dependency, create_webui_router, require_permission
from .store import WebUiStore

__all__ = [
    "PermissionEngine",
    "PermissionRegistry",
    "WebUiPrincipal",
    "WebUiStore",
    "WebUiFeature",
    "build_default_registry",
    "create_principal_dependency",
    "create_webui_router",
    "require_permission",
]
