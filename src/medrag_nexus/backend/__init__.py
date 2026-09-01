"""后端账号、注册、权限、知识域与 Agent 应用层。"""

from .account_router import AccountPrincipal, create_account_router, create_principal_dependency, require_permission
from .account_store import AccountStore
from .feature import BackendFeature
from .permissions import PermissionEngine, PermissionRegistry, build_default_registry

__all__ = [
    "PermissionEngine",
    "PermissionRegistry",
    "AccountPrincipal",
    "AccountStore",
    "BackendFeature",
    "build_default_registry",
    "create_principal_dependency",
    "create_account_router",
    "require_permission",
]
