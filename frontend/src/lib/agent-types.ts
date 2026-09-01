export type AgentActionStatus =
  | "pending_confirmation"
  | "pending_input"
  | "confirmed"
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "expired";

export type AgentRiskLevel = "read" | "write" | "sensitive" | "destructive";

export interface AgentConfirmationSpec {
  mode: "click" | "type_name";
  expected_label?: string | null;
  prompt?: string | null;
}

export interface AgentActionDetail {
  label: string;
  value: string;
}

export interface AgentActionCardData {
  kind: "action";
  action_id: string;
  tool_name?: string;
  title: string;
  summary?: string;
  details?: AgentActionDetail[];
  risk_level?: AgentRiskLevel;
  status: AgentActionStatus;
  confirmation?: AgentConfirmationSpec;
  result_summary?: string | null;
  error?: string | null;
  expires_at?: string | null;
}

export interface AgentSecureField {
  name: string;
  label: string;
  min_length?: number;
  autocomplete?: string;
}

export interface AgentInputRequestData {
  kind: "input";
  action_id: string;
  input_type: "password" | "file";
  title: string;
  description?: string;
  status: "pending" | "submitting" | "submitted" | "cancelled" | "failed";
  fields?: AgentSecureField[];
  accept?: string;
  multiple?: boolean;
  max_files?: number;
  error?: string | null;
}

export interface AgentArtifactData {
  kind: "artifact";
  artifact_id: string;
  file_name: string;
  mime_type?: string;
  size_bytes?: number;
  expires_at?: string | null;
  revoked_at?: string | null;
  status?: "available" | "revoked" | "expired";
}

export interface AgentTaskData {
  kind: "task";
  task_id: string;
  label?: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  stage?: string;
  percent?: number;
  message?: string;
}

export interface AgentResultData {
  kind: "result";
  title?: string;
  message: string;
  status: "success" | "error" | "info";
  code?: string;
}

export type AgentInteraction =
  | AgentActionCardData
  | AgentInputRequestData
  | AgentArtifactData
  | AgentTaskData
  | AgentResultData;

export interface AgentActionResponse {
  action_id: string;
  status: AgentActionStatus | "pending" | "executing";
  result_summary?: unknown;
  error?: string | null;
  task?: Omit<AgentTaskData, "kind"> | null;
  artifact?: Omit<AgentArtifactData, "kind"> | null;
  input?: Omit<AgentInputRequestData, "kind" | "status"> | null;
}
