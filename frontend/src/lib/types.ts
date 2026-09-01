export type Status = "ok" | "degraded" | "unavailable";

export interface TaskAccepted {
  task_id: string;
  status: "queued";
}

export interface TaskProgress {
  current: number;
  total: number;
  percent: number;
}

export interface TaskResponse {
  task_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  progress: TaskProgress;
  result?: Record<string, unknown> | null;
  error?: Record<string, unknown> | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  modified_at: string;
}

export interface WorkspaceListItem {
  workspace_id: string;
  workspace_name: string;
  resource_count: number;
  file_count: number;
  str_count: number;
  total_size_bytes: number;
  created_at: string;
  modified_at: string;
}

export interface WorkspaceListResponse {
  user_id: string;
  workspaces: WorkspaceListItem[];
}

export interface UserListItem {
  user_id: string;
  user_name: string;
  workspace_count: number;
  resource_count: number;
  file_count: number;
  str_count: number;
  total_size_bytes: number;
}

export interface UserCreateRequest {
  user_id: string;
  user_name: string;
}

export interface UserListResponse {
  users: UserListItem[];
}

export interface FileListItem {
  file_id: string;
  file_name: string;
  content_hash: string;
  size_bytes: number;
  created_at: string;
  modified_at: string;
}

export interface StringListItem {
  content?: string | null;
  content_hash: string;
  size_bytes: number;
  created_at: string;
  modified_at: string;
}

export interface WorkspaceStats {
  resource_count: number;
  file_count: number;
  str_count: number;
  total_size_bytes: number;
}

export interface FileListResponse {
  workspace_id: string;
  files: FileListItem[];
  strings: StringListItem[];
  stats: WorkspaceStats;
}

export interface RetrievalScores {
  vector?: number | null;
  bm25?: number | null;
  rrf?: number | null;
  rerank?: number | null;
}

export interface RetrievalItem {
  rank: number;
  chunk_id: string;
  user_id: string;
  workspace_id: string;
  source_type: "file" | "str";
  file_id?: string;
  file_name?: string;
  content: string;
  section?: string;
  page_number?: number;
  scores: RetrievalScores;
  matched_by: Array<"vector" | "bm25">;
}

export interface WarningItem {
  code: string;
  message: string;
}

export interface RetrievalResponse {
  query: string;
  top_k: number;
  count: number;
  degraded: boolean;
  warnings: WarningItem[];
  items: RetrievalItem[];
}

export interface DependencyState {
  status: Status;
  latency_ms?: number | null;
  error?: string | null;
}

export interface HealthResponse {
  status: Status;
  dependencies?: Record<string, DependencyState> | null;
}

export interface Workspace {
  userId: string;
  workspaceId: string;
  workspaceName: string;
}

export interface RecentTask {
  taskId: string;
  userId: string;
  label: string;
  createdAt: string;
}
