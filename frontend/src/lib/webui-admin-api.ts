import { type PermissionGroupDefinition, WebUiApiError } from "@/lib/webui-api";

const WEBUI_API_ROOT = "/backend/api/webui/v1";

interface ErrorEnvelope {
  detail?: string | { code?: string; message?: string };
  error?: { code?: string; message?: string; request_id?: string };
}

export interface AdminAccount {
  account_id: string;
  login_name: string;
  display_name: string;
  permission_level: number;
  enabled: boolean;
  bound_user_id: string | null;
  must_change_password: boolean;
  password_changed_at: string;
  last_login_at: string | null;
  created_at: string;
  modified_at: string;
  groups: string[];
  bound_user_ids: string[];
  permissions?: string[];
  capabilities?: {
    can_update?: boolean;
    can_reset_password?: boolean;
    can_bind_user?: boolean;
    protected?: boolean;
  };
}

export interface PermissionGroupInput {
  key: string;
  name?: string;
  description: string;
  permissions: string[];
}

export interface AdminAccountList {
  accounts: AdminAccount[];
  total: number;
}

export interface CreateAdminAccountInput {
  login_name: string;
  display_name: string;
  password: string;
  permission_level: number;
  group_keys: string[];
  must_change_password: boolean;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${WEBUI_API_ROOT}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
    cache: "no-store",
    credentials: "same-origin",
  });
  const body = (await response.json().catch(() => null)) as T | ErrorEnvelope | null;
  if (!response.ok) {
    const envelope = body as ErrorEnvelope | null;
    throw new WebUiApiError(
      envelope?.error?.message ?? (
        typeof envelope?.detail === "object" ? envelope.detail.message : envelope?.detail
      ) ?? `请求失败，HTTP ${response.status}`,
      response.status,
      envelope ?? undefined,
    );
  }
  if (body === null) throw new WebUiApiError("服务端返回了空响应", 502);
  return body as T;
}

export function listWebUiAccounts(): Promise<AdminAccountList> {
  return request("/accounts");
}

export function createWebUiAccount(payload: CreateAdminAccountInput): Promise<AdminAccount> {
  return request("/accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function patchWebUiAccount(
  accountId: string,
  payload: {
    display_name: string;
    permission_level: number;
    enabled: boolean;
    group_keys: string[];
  },
): Promise<AdminAccount> {
  return request(`/accounts/${encodeURIComponent(accountId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function bindWebUiAccount(
  accountId: string,
  userId: string,
  bound = true,
): Promise<{ account_id: string; bound_user_id: string | null; bound_user_ids: string[] }> {
  return request(`/accounts/${encodeURIComponent(accountId)}/binding`, {
    method: "PUT",
    body: JSON.stringify({ user_id: userId, bound }),
  });
}

export function setWebUiAccountBindings(
  accountId: string,
  userIds: string[],
): Promise<{ account_id: string; bound_user_id: string | null; bound_user_ids: string[] }> {
  return request(`/accounts/${encodeURIComponent(accountId)}/bindings`, {
    method: "PUT",
    body: JSON.stringify({ user_ids: userIds }),
  });
}

export function createPermissionGroup(payload: PermissionGroupInput): Promise<PermissionGroupDefinition> {
  return request("/permission-groups", {
    method: "POST",
    body: JSON.stringify({
      group_key: payload.key,
      name: payload.name,
      description: payload.description,
      permissions: payload.permissions,
    }),
  });
}

export function patchPermissionGroup(
  groupKey: string,
  payload: Omit<PermissionGroupInput, "key">,
): Promise<PermissionGroupDefinition> {
  return request(`/permission-groups/${encodeURIComponent(groupKey)}`, {
    method: "PATCH",
    body: JSON.stringify({
      name: payload.name,
      description: payload.description,
      permissions: payload.permissions,
    }),
  });
}

export function deletePermissionGroup(groupKey: string): Promise<{ status: "deleted" }> {
  return request(`/permission-groups/${encodeURIComponent(groupKey)}`, { method: "DELETE" });
}

export function leaveOwnPermissionGroup(groupKey: string): Promise<AdminAccount> {
  return request(`/account/permission-groups/${encodeURIComponent(groupKey)}`, { method: "DELETE" });
}

export function resetWebUiAccountPassword(
  accountId: string,
  payload: { new_password: string; must_change_password: boolean },
): Promise<{ status: "ok" }> {
  return request(`/accounts/${encodeURIComponent(accountId)}/password/reset`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function changeOwnWebUiPassword(payload: {
  current_password: string;
  new_password: string;
}): Promise<{ status: "ok" }> {
  return request("/account/password", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
