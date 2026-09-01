import type { WebUiPrincipal } from "@/lib/webui-api";

export const PAGE_PERMISSIONS = {
  documents: [
    "webui.workspace.read",
    "webui.workspace.create",
    "webui.workspace.rename",
    "webui.workspace.delete",
    "webui.resource.file.add",
    "webui.resource.file.delete",
    "webui.resource.text.add",
    "webui.resource.text.delete",
  ],
  retrieval: ["webui.retrieval.use"],
  chat: ["webui.chat.use"],
  management: ["webui.account.update_self", "webui.user.read", "webui.workspace.read"],
  system: ["webui.system.read", "webui.account.manage"],
} as const;

export type PageCapability = keyof typeof PAGE_PERMISSIONS;

function hasPermission(principal: WebUiPrincipal, permission: string) {
  const granted = new Set(principal.permissions);
  if (granted.has("*") || granted.has(permission)) return true;
  const parts = permission.split(".");
  while (parts.length > 1) {
    parts.pop();
    if (granted.has(`${parts.join(".")}.*`)) return true;
  }
  return false;
}

export function canAccessPage(principal: WebUiPrincipal, page: PageCapability) {
  return PAGE_PERMISSIONS[page].some((permission) => hasPermission(principal, permission));
}

export function defaultAccountRoute(principal: WebUiPrincipal) {
  if (principal.account.must_change_password) return "/login";
  if (
    hasPermission(principal, "webui.account.manage") &&
    canAccessPage(principal, "system")
  ) return "/system";
  for (const [page, path] of [
    ["documents", "/documents"],
    ["retrieval", "/retrieval"],
    ["chat", "/chat"],
    ["management", "/management"],
    ["system", "/system"],
  ] as const) {
    if (canAccessPage(principal, page)) return path;
  }
  return "/login";
}
