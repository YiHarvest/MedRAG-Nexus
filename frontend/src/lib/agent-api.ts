import type { AgentActionResponse } from "@/lib/agent-types";

const AGENT_API_ROOT = "/backend/api/v1/agent";

interface ErrorEnvelope {
  detail?: string | { code?: string; message?: string };
  error?: { code?: string; message?: string; request_id?: string };
}

export class AgentApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code = "agent_request_failed") {
    super(message);
    this.name = "AgentApiError";
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${AGENT_API_ROOT}${path}`, {
    ...init,
    headers,
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ErrorEnvelope | null;
    const detail = typeof body?.detail === "object" ? body.detail.message : body?.detail;
    throw new AgentApiError(
      body?.error?.message ?? detail ?? `请求失败，HTTP ${response.status}`,
      response.status,
      body?.error?.code ?? (typeof body?.detail === "object" ? body.detail.code : undefined),
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function describeAgentError(reason: unknown): string {
  return reason instanceof Error ? reason.message : "操作失败，请稍后重试。";
}

export function confirmAgentAction(actionId: string, confirmationText?: string): Promise<AgentActionResponse> {
  return request(`/actions/${encodeURIComponent(actionId)}/confirm`, {
    method: "POST",
    body: JSON.stringify(confirmationText ? { confirmation_text: confirmationText } : {}),
  });
}

export function cancelAgentAction(actionId: string): Promise<AgentActionResponse | void> {
  return request(`/actions/${encodeURIComponent(actionId)}`, { method: "DELETE" });
}

export function submitAgentSecureInput(
  actionId: string,
  values: Record<string, string>,
): Promise<AgentActionResponse> {
  return request(`/actions/${encodeURIComponent(actionId)}/input`, {
    method: "POST",
    body: JSON.stringify({ values }),
  });
}

export function uploadAgentFiles(actionId: string, files: File[]): Promise<AgentActionResponse> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  return request(`/actions/${encodeURIComponent(actionId)}/input`, { method: "POST", body: form });
}

export function artifactDownloadUrl(artifactId: string): string {
  return `${AGENT_API_ROOT}/artifacts/${encodeURIComponent(artifactId)}/download`;
}

export function revokeAgentArtifact(artifactId: string): Promise<void> {
  return request(`/artifacts/${encodeURIComponent(artifactId)}`, { method: "DELETE" });
}

