import type {
  FileListResponse,
  HealthResponse,
  RetrievalResponse,
  TaskAccepted,
  TaskResponse,
  UserListResponse,
  UserCreateRequest,
  UserListItem,
  WorkspaceListResponse,
} from "@/lib/types";

const API_ROOT = "/backend/api/v1";

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    request_id?: string;
    details?: unknown;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly details?: unknown;

  constructor(message: string, status: number, body?: ErrorEnvelope) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.error?.code ?? "request_failed";
    this.requestId = body?.error?.request_id;
    this.details = body?.error?.details;
  }
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json().catch(() => null)) as T | ErrorEnvelope | null;
  if (!response.ok) {
    const envelope = body as ErrorEnvelope | null;
    throw new ApiError(
      envelope?.error?.message ?? `请求失败，HTTP ${response.status}`,
      response.status,
      envelope ?? undefined,
    );
  }
  if (body === null) {
    throw new ApiError("后端返回了空响应", 502, {
      error: { code: "invalid_backend_response" },
    });
  }
  return body as T;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  return readJson<T>(
    await fetch(`${API_ROOT}${path}`, {
      ...init,
      headers,
      cache: "no-store",
    }),
  );
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.requestId ? `${error.message}，请求 ID：${error.requestId}` : error.message;
  }
  return error instanceof Error ? error.message : "发生未知错误";
}

export async function addKnowledge(form: FormData): Promise<TaskAccepted> {
  return request<TaskAccepted>("/add", { method: "POST", body: form });
}

export async function listUsers(): Promise<UserListResponse> {
  return request<UserListResponse>("/users");
}

export async function createUser(payload: UserCreateRequest): Promise<UserListItem> {
  return request<UserListItem>("/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function listWorkspaces(userId: string): Promise<WorkspaceListResponse> {
  return request<WorkspaceListResponse>(`/users/${encodeURIComponent(userId)}/workspaces`);
}

export async function listFiles(
  userId: string,
  workspaceId: string,
  includeStringContent = false,
): Promise<FileListResponse> {
  const query = new URLSearchParams({
    user_id: userId,
    include_string_content: String(includeStringContent),
  });
  return request<FileListResponse>(`/workspaces/${encodeURIComponent(workspaceId)}/files?${query.toString()}`);
}

export async function deleteFile(payload: {
  user_id: string;
  workspace_id: string;
  file_id: string;
  file_name: string;
  callback_url?: string;
}): Promise<TaskAccepted> {
  return request<TaskAccepted>("/delete", { method: "POST", body: JSON.stringify(payload) });
}

export async function deleteString(payload: {
  user_id: string;
  workspace_id: string;
  content_hash: string;
  callback_url?: string;
}): Promise<TaskAccepted> {
  return request<TaskAccepted>("/delete-string", { method: "POST", body: JSON.stringify(payload) });
}

export async function retrieve(payload: {
  user_id: string;
  workspace_id: string;
  query: string;
  top_k: number;
}): Promise<RetrievalResponse> {
  return request<RetrievalResponse>("/retrieval", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getTask(taskId: string, userId: string): Promise<TaskResponse> {
  const query = new URLSearchParams({ user_id: userId });
  return request<TaskResponse>(`/tasks/${encodeURIComponent(taskId)}?${query.toString()}`);
}

export async function openChatStream(payload: {
  user_id: string;
  messages: Array<{ role: "user" | "assistant"; content: string }>;
  top_k?: number;
  conversation_id?: string | null;
}, signal?: AbortSignal): Promise<Response> {
  const response = await fetch(`${API_ROOT}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    await readJson<never>(response);
  }
  if (!response.body) {
    throw new ApiError("聊天服务没有返回数据流", 502, {
      error: { code: "missing_response_stream" },
    });
  }
  return response;
}

export async function getHealth(kind: "live" | "ready", details = false): Promise<HealthResponse> {
  const suffix = kind === "ready" ? `?details=${String(details)}` : "";
  const response = await fetch(`${API_ROOT}/health/${kind}${suffix}`, { cache: "no-store" });
  if (kind === "ready" && response.status === 503) {
    return (await response.json()) as HealthResponse;
  }
  return readJson<HealthResponse>(response);
}
