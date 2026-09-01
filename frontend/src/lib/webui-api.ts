import type {
  FileListResponse,
  HealthResponse,
  RetrievalResponse,
  TaskAccepted,
  TaskResponse,
  WorkspaceListItem,
} from "@/lib/types";

const WEBUI_API_ROOT = "/backend/api/webui/v1";

export interface WebUiAccount {
  account_id: string;
  login_name: string;
  display_name: string;
  permission_level: number;
  groups: string[];
  bound_user_ids: string[];
  bound_user_id: string | null;
  must_change_password?: boolean;
}

export interface WebUiPrincipal {
  account: WebUiAccount;
  permissions: string[];
}

export interface WorkspaceCapabilities {
  can_read: boolean;
  can_add_file: boolean;
  can_download_file: boolean;
  can_add_text: boolean;
  can_delete_file: boolean;
  can_delete_text: boolean;
  can_add_resource: boolean;
  can_delete_resource: boolean;
  can_rename: boolean;
  can_delete_workspace: boolean;
  can_manage_policy: boolean;
}

export interface WebUiWorkspace extends WorkspaceListItem {
  user_id: string;
  read_min_level: number;
  cud_min_level: number;
  policy_version: number;
  capabilities: WorkspaceCapabilities;
}

export interface WebUiWorkspaceUser {
  user_id: string;
  user_name: string;
  read_min_level: number;
  workspace_create_min_level: number;
  policy_version: number;
  can_create_workspace: boolean;
  can_manage_policy: boolean;
  can_rename: boolean;
  can_delete: boolean;
}

export interface WebUiWorkspaceList {
  workspaces: WebUiWorkspace[];
  users: WebUiWorkspaceUser[];
}

export type PolicyResourceType = "user" | "workspace";
export type PolicyBindingAction = string;
export type PolicyPrincipalType = "account" | "group";
export type PolicyBindingEffect = "allow" | "deny";

export interface PolicyBinding {
  principal_type: PolicyPrincipalType;
  principal_id: string;
  effect: PolicyBindingEffect;
  immutable?: boolean;
  managed_by?: string | null;
}

export interface PolicyBindingsResponse {
  resource_type: PolicyResourceType;
  resource_id: string;
  bindings: Partial<Record<PolicyBindingAction, PolicyBinding[]>>;
}

export interface PermissionLevelDefinition {
  level: number;
  key: string;
  label: string;
  description: string;
  plugin_id: string;
  available: boolean;
  permissions?: string[];
}

export interface PermissionNodeDefinition {
  key: string;
  label?: string;
  description: string;
  plugin_id: string;
  available: boolean;
  custom_assignable: boolean;
}

export interface PermissionGroupDefinition {
  key: string;
  name?: string;
  description: string;
  permissions: string[];
  system: boolean;
  immutable: boolean;
}

export interface PermissionPluginDefinition {
  plugin_id: string;
  name?: string;
  version?: string;
  available?: boolean;
  enabled?: boolean;
}

export interface PermissionCatalog {
  levels: PermissionLevelDefinition[];
  nodes: PermissionNodeDefinition[];
  groups: PermissionGroupDefinition[];
  plugins: PermissionPluginDefinition[];
}

interface PolicyBindingsWireResponse {
  resource_type: PolicyResourceType;
  resource_id: string;
  actions?: Partial<Record<PolicyBindingAction, PolicyBinding[]>>;
  bindings?: Partial<Record<PolicyBindingAction, PolicyBinding[]>>;
}

interface PermissionCatalogWireResponse {
  levels?: Array<PermissionLevelDefinition | {
    value: number;
    name: string;
    description: string;
    permissions?: string[];
  }>;
  permission_nodes?: PermissionNodeDefinition[];
  nodes?: PermissionNodeDefinition[];
  permission_groups?: PermissionGroupDefinition[];
  groups?: Array<PermissionGroupDefinition | {
    key: string;
    name?: string;
    description: string;
    permissions: string[];
    system_managed: boolean;
  }>;
  plugins?: PermissionPluginDefinition[];
}

interface ErrorEnvelope {
  detail?: string | {
    code?: string;
    message?: string;
  };
  error?: {
    code?: string;
    message?: string;
    request_id?: string;
    details?: Record<string, unknown>;
  };
}

export class WebUiApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly details?: Record<string, unknown>;

  constructor(message: string, status: number, body?: ErrorEnvelope) {
    super(message);
    this.name = "WebUiApiError";
    this.status = status;
    this.code = body?.error?.code ?? (
      typeof body?.detail === "object" ? body.detail.code : undefined
    ) ?? "webui_request_failed";
    this.requestId = body?.error?.request_id;
    this.details = body?.error?.details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${WEBUI_API_ROOT}${path}`, {
    ...init,
    headers,
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
  if (body === null) {
    throw new WebUiApiError("服务端返回了空响应", 502);
  }
  return body as T;
}

export function getCurrentAccount(): Promise<WebUiPrincipal> {
  return request<WebUiPrincipal>("/auth/me");
}

export function listAccessibleWorkspaces(): Promise<WebUiWorkspaceList> {
  return request<WebUiWorkspaceList>("/workspaces");
}

export async function getPermissionCatalog(): Promise<PermissionCatalog> {
  const response = await request<PermissionCatalogWireResponse>("/permission-catalog");
  const levels = (response.levels ?? []).map((item) => "value" in item ? {
    level: item.value,
    key: String(item.value),
    label: item.name,
    description: item.description,
    plugin_id: "webui.core",
    available: true,
    permissions: item.permissions ?? [],
  } : item);
  const groups = (response.groups ?? response.permission_groups ?? []).map((item) => "system_managed" in item ? {
    key: item.key,
    name: item.name,
    description: item.description,
    permissions: item.permissions,
    system: item.system_managed,
    immutable: item.system_managed,
  } : item);
  return {
    levels,
    nodes: response.nodes ?? response.permission_nodes ?? [],
    groups,
    plugins: response.plugins ?? [],
  };
}

export function createWebUiKnowledgeUser(payload: {
  user_name: string;
  bind_account_id?: string | null;
}): Promise<WebUiWorkspaceUser> {
  return request<WebUiWorkspaceUser>("/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function renameWebUiKnowledgeUser(userId: string, userName: string): Promise<{
  user_id: string;
  user_name: string;
}> {
  return request(`/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: JSON.stringify({ user_name: userName }),
  });
}

export function leaveWebUiKnowledgeUser(userId: string): Promise<{ status: "ok"; user_id: string }> {
  return request(`/users/${encodeURIComponent(userId)}/access`, { method: "DELETE" });
}

export function createWebUiWorkspace(payload: {
  user_id: string;
  workspace_name: string;
  read_min_level: number;
  cud_min_level: number;
}): Promise<WebUiWorkspace> {
  return request<WebUiWorkspace>("/workspaces", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function renameWebUiWorkspace(workspaceId: string, workspaceName: string): Promise<WebUiWorkspace> {
  return request<WebUiWorkspace>(`/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "PATCH",
    body: JSON.stringify({ workspace_name: workspaceName }),
  });
}

export function leaveWebUiWorkspace(workspaceId: string): Promise<{ status: "ok"; workspace_id: string }> {
  return request(`/workspaces/${encodeURIComponent(workspaceId)}/access`, { method: "DELETE" });
}

export function updateWebUiWorkspacePolicy(workspaceId: string, payload: {
  read_min_level: number;
  cud_min_level: number;
}): Promise<WebUiWorkspace> {
  return request<WebUiWorkspace>(`/workspaces/${encodeURIComponent(workspaceId)}/policy`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function updateWebUiUserPolicy(userId: string, payload: {
  read_min_level: number;
  workspace_create_min_level: number;
}): Promise<{
  user_id: string;
  read_min_level: number;
  workspace_create_min_level: number;
  policy_version: number;
}> {
  return request(`/users/${encodeURIComponent(userId)}/policy`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

function policyBindingsPath(resourceType: PolicyResourceType, resourceId: string): string {
  const collection = resourceType === "user" ? "users" : "workspaces";
  return `/${collection}/${encodeURIComponent(resourceId)}/bindings`;
}

function normalizePolicyBindings(response: PolicyBindingsWireResponse): PolicyBindingsResponse {
  return {
    resource_type: response.resource_type,
    resource_id: response.resource_id,
    bindings: response.bindings ?? response.actions ?? {},
  };
}

export async function getWebUiPolicyBindings(
  resourceType: PolicyResourceType,
  resourceId: string,
): Promise<PolicyBindingsResponse> {
  const response = await request<PolicyBindingsWireResponse>(policyBindingsPath(resourceType, resourceId));
  return normalizePolicyBindings(response);
}

export async function updateWebUiPolicyBindings(
  resourceType: PolicyResourceType,
  resourceId: string,
  payload: { action: PolicyBindingAction; bindings: PolicyBinding[] },
): Promise<PolicyBindingsResponse> {
  const response = await request<PolicyBindingsWireResponse>(policyBindingsPath(resourceType, resourceId), {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  return normalizePolicyBindings(response);
}

export function deleteWebUiWorkspace(workspaceId: string, confirmName: string): Promise<{
  status: "deleted" | "cleanup_pending";
  workspace_id: string;
}> {
  const query = new URLSearchParams({ confirm_name: confirmName });
  return request(`/workspaces/${encodeURIComponent(workspaceId)}?${query.toString()}`, {
    method: "DELETE",
  });
}

export function deleteWebUiKnowledgeUser(userId: string, confirmName: string): Promise<{
  status: "deleted" | "cleanup_pending";
  user_id: string;
  deleted_workspace_count: number;
}> {
  const query = new URLSearchParams({ confirm_name: confirmName });
  return request(`/users/${encodeURIComponent(userId)}?${query.toString()}`, {
    method: "DELETE",
  });
}

export function listWebUiFiles(workspaceId: string, includeStringContent = false): Promise<FileListResponse> {
  const query = new URLSearchParams({ include_string_content: String(includeStringContent) });
  return request(`/workspaces/${encodeURIComponent(workspaceId)}/files?${query.toString()}`);
}

export function addWebUiResource(workspaceId: string, form: FormData): Promise<TaskAccepted> {
  return request(`/workspaces/${encodeURIComponent(workspaceId)}/resources`, {
    method: "POST",
    body: form,
  });
}

export function deleteWebUiFile(workspaceId: string, fileId: string): Promise<TaskAccepted> {
  return request(
    `/workspaces/${encodeURIComponent(workspaceId)}/files/${encodeURIComponent(fileId)}`,
    { method: "DELETE" },
  );
}

export function deleteWebUiString(workspaceId: string, contentHash: string): Promise<TaskAccepted> {
  return request(
    `/workspaces/${encodeURIComponent(workspaceId)}/strings/${encodeURIComponent(contentHash)}`,
    { method: "DELETE" },
  );
}

export function retrieveWebUiKnowledge(payload: {
  workspace_id: string;
  query: string;
  top_k: number;
}): Promise<RetrievalResponse> {
  return request("/retrieval", { method: "POST", body: JSON.stringify(payload) });
}

export function getWebUiTask(taskId: string): Promise<TaskResponse> {
  return request(`/tasks/${encodeURIComponent(taskId)}`);
}

export function cancelWebUiTask(taskId: string): Promise<TaskResponse> {
  return request(`/tasks/${encodeURIComponent(taskId)}`, { method: "DELETE" });
}

export function getWebUiHealth(): Promise<HealthResponse> {
  return request("/system/health");
}

export async function openWebUiChatStream(payload: {
  messages: Array<{ role: "user" | "assistant"; content: string }>;
  top_k?: number;
  conversation_id?: string | null;
}, signal?: AbortSignal): Promise<Response> {
  const response = await fetch(`${WEBUI_API_ROOT}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    cache: "no-store",
    credentials: "same-origin",
    signal,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ErrorEnvelope | null;
    throw new WebUiApiError(
      body?.error?.message ?? (
        typeof body?.detail === "object" ? body.detail.message : body?.detail
      ) ?? `聊天请求失败，HTTP ${response.status}`,
      response.status,
      body ?? undefined,
    );
  }
  if (!response.body) throw new WebUiApiError("聊天服务没有返回数据流", 502);
  return response;
}

export function loginWebUiAccount(payload: {
  login_name: string;
  password: string;
}): Promise<WebUiPrincipal> {
  return request<WebUiPrincipal>("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function registerWebUiAccount(payload: {
  login_name: string;
  display_name: string;
  password: string;
}): Promise<WebUiPrincipal> {
  return request<WebUiPrincipal>("/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function logoutWebUiAccount(): Promise<void> {
  const response = await fetch(`${WEBUI_API_ROOT}/auth/logout`, {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!response.ok && response.status !== 401) {
    const body = (await response.json().catch(() => null)) as ErrorEnvelope | null;
    throw new WebUiApiError(
      body?.error?.message ?? (
        typeof body?.detail === "object" ? body.detail.message : body?.detail
      ) ?? `退出失败，HTTP ${response.status}`,
      response.status,
      body ?? undefined,
    );
  }
}

export function describeWebUiError(error: unknown) {
  if (error instanceof WebUiApiError) {
    return error.requestId ? `${error.message}，请求 ID：${error.requestId}` : error.message;
  }
  return error instanceof Error ? error.message : "发生未知错误";
}
