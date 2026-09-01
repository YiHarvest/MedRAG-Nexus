"use client";

import { Select, SelectItem } from "@carbon/react";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useAccount } from "@/components/account";
import {
  describeWebUiError,
  listAccessibleWorkspaces,
  type WebUiWorkspace,
  type WebUiWorkspaceUser,
  type WorkspaceCapabilities,
} from "@/lib/webui-api";
import type { Workspace } from "@/lib/types";

const WORKSPACE_KEY = "jd-knowledge-workspace-v3";
const EMPTY_WORKSPACE: Workspace = { userId: "", workspaceId: "", workspaceName: "" };

interface WorkspaceContextValue {
  workspace: Workspace;
  setWorkspace: (workspace: Workspace) => void;
  activateWorkspace: (workspace: WebUiWorkspace) => void;
  authenticated: boolean;
  hydrated: boolean;
  workspaceOptions: WebUiWorkspace[];
  workspaceUsers: WebUiWorkspaceUser[];
  workspaceCapabilities: WorkspaceCapabilities | null;
  workspacesLoading: boolean;
  workspacesError: string | null;
  refreshWorkspaces: () => Promise<WebUiWorkspace[]>;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { account, authenticated, loading: accountLoading } = useAccount();
  const [workspace, setWorkspaceState] = useState<Workspace>(EMPTY_WORKSPACE);
  const [workspaceHydrated, setWorkspaceHydrated] = useState(false);
  const [workspaceOptions, setWorkspaceOptions] = useState<WebUiWorkspace[]>([]);
  const [workspaceUsers, setWorkspaceUsers] = useState<WebUiWorkspaceUser[]>([]);
  const [workspaceScopeAccountId, setWorkspaceScopeAccountId] = useState("");
  const [workspacesLoading, setWorkspacesLoading] = useState(false);
  const [workspacesError, setWorkspacesError] = useState<string | null>(null);
  const scopedWorkspaceOptions = useMemo(
    () => workspaceScopeAccountId === account?.account_id ? workspaceOptions : [],
    [account?.account_id, workspaceOptions, workspaceScopeAccountId],
  );
  const scopedWorkspaceUsers = useMemo(
    () => workspaceScopeAccountId === account?.account_id ? workspaceUsers : [],
    [account?.account_id, workspaceScopeAccountId, workspaceUsers],
  );
  const userId = scopedWorkspaceUsers[0]?.user_id ?? "";

  const setWorkspace = useCallback((next: Workspace) => {
    setWorkspaceState((current) => {
      if (!next.workspaceId) {
        const cleared = { userId: next.userId || userId, workspaceId: "", workspaceName: "" };
        window.localStorage.removeItem(WORKSPACE_KEY);
        return cleared;
      }
      const allowed = scopedWorkspaceOptions.find(
        (item) => item.workspace_id === next.workspaceId && item.user_id === next.userId && item.capabilities.can_read,
      );
      if (!allowed) return current;
      const selected = {
        userId: allowed.user_id,
        workspaceId: allowed.workspace_id,
        workspaceName: allowed.workspace_name,
      };
      window.localStorage.setItem(WORKSPACE_KEY, JSON.stringify(selected));
      return selected;
    });
  }, [userId, scopedWorkspaceOptions]);

  const activateWorkspace = useCallback((item: WebUiWorkspace) => {
    if (!item.capabilities.can_read) return;
    const next = {
      userId: item.user_id,
      workspaceId: item.workspace_id,
      workspaceName: item.workspace_name,
    };
    window.localStorage.setItem(WORKSPACE_KEY, JSON.stringify(next));
    setWorkspaceState(next);
  }, []);

  const refreshWorkspaces = useCallback(async () => {
    if (!account) {
      setWorkspaceOptions([]);
      setWorkspaceUsers([]);
      setWorkspaceScopeAccountId("");
      setWorkspaceState(EMPTY_WORKSPACE);
      setWorkspaceHydrated(true);
      return [];
    }
    setWorkspacesLoading(true);
    setWorkspacesError(null);
    try {
      const response = await listAccessibleWorkspaces();
      const allowed = response.workspaces.filter((item) => item.capabilities.can_read);
      setWorkspaceOptions(allowed);
      setWorkspaceUsers(response.users);
      setWorkspaceScopeAccountId(account.account_id);
      setWorkspaceState((current) => {
        let saved: Workspace | null = null;
        try {
          saved = JSON.parse(window.localStorage.getItem(WORKSPACE_KEY) ?? "null") as Workspace | null;
        } catch {
          window.localStorage.removeItem(WORKSPACE_KEY);
        }
        const candidateId = current.workspaceId || saved?.workspaceId || "";
        const selected = allowed.find((item) => item.workspace_id === candidateId);
        if (!selected) {
          window.localStorage.removeItem(WORKSPACE_KEY);
          return { userId: response.users[0]?.user_id ?? "", workspaceId: "", workspaceName: "" };
        }
        const next = {
          userId: selected.user_id,
          workspaceId: selected.workspace_id,
          workspaceName: selected.workspace_name,
        };
        window.localStorage.setItem(WORKSPACE_KEY, JSON.stringify(next));
        return next;
      });
      return allowed;
    } catch (error) {
      setWorkspaceOptions([]);
      setWorkspaceUsers([]);
      setWorkspaceScopeAccountId(account.account_id);
      setWorkspaceState(EMPTY_WORKSPACE);
      window.localStorage.removeItem(WORKSPACE_KEY);
      setWorkspacesError(describeWebUiError(error));
      return [];
    } finally {
      setWorkspacesLoading(false);
      setWorkspaceHydrated(true);
    }
  }, [account]);

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshWorkspaces(), 0);
    return () => window.clearTimeout(timer);
  }, [refreshWorkspaces]);

  const effectiveWorkspace = useMemo(
    () => !workspace.workspaceId || scopedWorkspaceOptions.some(
      (item) => item.workspace_id === workspace.workspaceId && item.user_id === workspace.userId,
    )
      ? workspace
      : { userId, workspaceId: "", workspaceName: "" },
    [workspace, scopedWorkspaceOptions, userId],
  );
  const value = useMemo(
    () => ({
      workspace: effectiveWorkspace,
      setWorkspace,
      activateWorkspace,
      authenticated,
      hydrated: !accountLoading && workspaceHydrated,
      workspaceOptions: scopedWorkspaceOptions,
      workspaceUsers: scopedWorkspaceUsers,
      workspaceCapabilities: scopedWorkspaceOptions.find(
        (item) => item.workspace_id === effectiveWorkspace.workspaceId,
      )?.capabilities ?? null,
      workspacesLoading,
      workspacesError,
      refreshWorkspaces,
    }),
    [
      effectiveWorkspace,
      setWorkspace,
      activateWorkspace,
      authenticated,
      accountLoading,
      workspaceHydrated,
      scopedWorkspaceOptions,
      scopedWorkspaceUsers,
      workspacesLoading,
      workspacesError,
      refreshWorkspaces,
    ],
  );
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace 必须在 WorkspaceProvider 中使用");
  return value;
}

export function WorkspaceBar() {
  const {
    workspace,
    setWorkspace,
    activateWorkspace,
    workspaceOptions,
    workspaceUsers,
    workspacesLoading,
    workspacesError,
  } = useWorkspace();

  return (
    <section className="workspace-bar workspace-bar-compact" aria-label="选择知识空间">
      <Select
        id="workspace-selector"
        labelText="知识库"
        value={workspace.workspaceId}
        disabled={workspacesLoading}
        onChange={(event) => {
          const selected = workspaceOptions.find((item) => item.workspace_id === event.target.value);
          if (selected) activateWorkspace(selected);
          else setWorkspace({ userId: workspace.userId, workspaceId: "", workspaceName: "" });
        }}
      >
        <SelectItem
          value=""
          text={workspacesLoading ? "正在加载…" : workspacesError ? "知识库加载失败" : "请选择知识库"}
        />
        {workspaceOptions.map((item) => (
          <SelectItem
            key={item.workspace_id}
            value={item.workspace_id}
            text={`${workspaceUsers.find((user) => user.user_id === item.user_id)?.user_name ?? item.user_id} · ${item.workspace_name}`}
          />
        ))}
      </Select>
    </section>
  );
}
