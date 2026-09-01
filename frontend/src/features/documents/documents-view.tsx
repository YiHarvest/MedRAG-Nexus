"use client";

import {
  Add,
  ChevronRight,
  Close,
  DocumentAdd,
  Renew,
  TextCreation,
  TrashCan,
  Upload,
} from "@carbon/icons-react";
import {
  Button,
  FileUploaderDropContainer,
  InlineNotification,
  ProgressBar,
  Select,
  SelectItem,
  TextArea,
  TextInput,
} from "@carbon/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { useDocumentMemory } from "@/components/app-memory";
import type { ActiveTask, FileUploadTask } from "@/components/app-memory";
import { useAccount } from "@/components/account";
import { PolicyBindingsEditor, type PolicyActionOption } from "@/components/policy-bindings-editor";
import { EmptyState, LoadingState } from "@/components/states";
import { useShellHeaderAction } from "@/components/shell-header-action";
import { useWorkspace } from "@/components/workspace";
import { clearUploadFiles, readUploadFiles, removeUploadFile, saveUploadFiles } from "@/lib/browser-persistence";
import { formatDate } from "@/lib/storage";
import { generateUUID } from "@/lib/uuid";
import {
  addWebUiResource,
  cancelWebUiTask,
  registerKnowledgeDomain,
  registerKnowledgeBase,
  deleteWebUiFile,
  deleteWebUiString,
  deleteWebUiWorkspace,
  describeWebUiError as describeError,
  getWebUiTask,
  listWebUiFiles,
  renameWebUiWorkspace,
  updateWebUiWorkspacePolicy,
  WebUiApiError,
} from "@/lib/webui-api";
import type { FileListItem, StringListItem, WorkspaceStats } from "@/lib/types";

const ACCEPTED_FILES = [".pdf", ".txt", ".docx"];
const MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024;

const WORKSPACE_POLICY_ACTIONS: PolicyActionOption[] = [
  {
    action: "webui.workspace.read",
    label: "读取知识库",
    description: "控制哪些账号或权限组能够查看、检索并通过 Agent 使用此知识库。",
  },
  {
    action: "webui.resource.file.add",
    label: "上传文件",
    description: "控制上传完整文件。",
  },
  {
    action: "webui.resource.file.download",
    label: "下载文件",
    description: "控制下载完整原始文件。",
  },
  {
    action: "webui.resource.file.delete",
    label: "删除文件",
    description: "控制删除整个文件。",
  },
  {
    action: "webui.resource.text.add",
    label: "添加文本",
    description: "控制添加整段文本。",
  },
  {
    action: "webui.resource.text.delete",
    label: "删除文本",
    description: "控制删除整段文本。",
  },
  {
    action: "webui.workspace.rename",
    label: "知识库改名",
    description: "控制修改 Workspace 展示名称。",
  },
  {
    action: "webui.workspace.delete",
    label: "整库删除",
    description: "控制删除 Workspace 及其全部存储数据。",
  },
  {
    action: "webui.workspace.policy.manage",
    label: "管理权限策略",
    description: "控制修改 Workspace 等级策略和 ACL。",
  },
];

type ResourceFilter = "all" | "file" | "str";

type SidebarResource =
  | { kind: "file"; key: string; item: FileListItem }
  | { kind: "str"; key: string; item: StringListItem };

export function DocumentsView() {
  const { account, can } = useAccount();
  const {
    workspace,
    setWorkspace,
    activateWorkspace,
    workspaceOptions,
    workspaceUsers,
    workspaceCapabilities,
    workspacesLoading: workspaceLoading,
    refreshWorkspaces,
  } = useWorkspace();
  const {
    persistenceReady,
    needsUploadRecovery,
    setNeedsUploadRecovery,
    sourceType,
    setSourceType,
    content,
    setContent,
    workspaceChoice,
    setWorkspaceChoice,
    newWorkspaceName,
    setNewWorkspaceName,
    setPendingWorkspaceId,
    workspaceConfirmed,
    setWorkspaceConfirmed,
    workspacePendingCreation,
    setWorkspacePendingCreation,
    submitting,
    setSubmitting,
    activeTask,
    setActiveTask,
    fileUploads,
    setFileUploads,
    uploadKey,
    setUploadKey,
    notice,
    setNotice,
  } = useDocumentMemory();
  const [files, setFiles] = useState<FileListItem[]>([]);
  const [strings, setStrings] = useState<StringListItem[]>([]);
  const [stats, setStats] = useState<WorkspaceStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [newWorkspaceUserId, setNewWorkspaceUserId] = useState("");
  const [selectedKnowledgeUserId, setSelectedKnowledgeUserId] = useState("");
  const [newKnowledgeUserOpen, setNewKnowledgeUserOpen] = useState(false);
  const [newKnowledgeUserName, setNewKnowledgeUserName] = useState("");
  const [readMinLevel, setReadMinLevel] = useState(0);
  const [cudMinLevel, setCudMinLevel] = useState(0);
  const [workspaceAction, setWorkspaceAction] = useState<"rename" | "delete" | "policy" | null>(null);
  const [workspaceActionValue, setWorkspaceActionValue] = useState("");
  const [workspaceActionBusy, setWorkspaceActionBusy] = useState(false);
  const [policyReadLevel, setPolicyReadLevel] = useState(0);
  const [policyCudLevel, setPolicyCudLevel] = useState(0);
  const [resourcePanelOpen, setResourcePanelOpen] = useState(false);
  const [resourceFilter, setResourceFilter] = useState<ResourceFilter>("all");
  const cancelledUploadIds = useRef(new Set<string>());
  const [cancellingUploadIds, setCancellingUploadIds] = useState<string[]>([]);

  const resources = useMemo<SidebarResource[]>(() => [
    ...files.map((item) => ({ kind: "file" as const, key: item.file_id, item })),
    ...strings.map((item) => ({
      kind: "str" as const,
      key: `${item.content_hash}-${item.created_at}`,
      item,
    })),
  ].sort((left, right) => Date.parse(right.item.modified_at) - Date.parse(left.item.modified_at)), [files, strings]);

  const filteredResources = resourceFilter === "all"
    ? resources
    : resources.filter((resource) => resource.kind === resourceFilter);
  const resourceCount = stats?.resource_count ?? resources.length;
  const creatableUsers = workspaceUsers.filter((item) => item.can_create_workspace);
  const preferredCreatableUser = creatableUsers[0];
  const selectedKnowledgeUser = workspaceUsers.find((item) => item.user_id === selectedKnowledgeUserId);
  const selectedCreatableUser = creatableUsers.find((item) => item.user_id === selectedKnowledgeUserId);
  const knowledgeUserWorkspaces = workspaceOptions.filter(
    (item) => item.user_id === selectedKnowledgeUserId,
  );
  const availableLevels = [0, 1, 2, 1000].filter(
    (level) => level <= (account?.permission_level ?? 0),
  );
  const selectedWorkspace = workspaceOptions.find(
    (item) => item.workspace_id === workspace.workspaceId,
  );
  const canAddFile = workspaceConfirmed && Boolean(workspaceCapabilities?.can_add_file);
  const canAddText = workspaceConfirmed && Boolean(workspaceCapabilities?.can_add_text);
  const canAddContent = canAddFile || canAddText;
  const uploadPersistenceKey = account?.account_id ?? "";

  const openResourcePanel = useCallback(() => {
    setResourcePanelOpen(true);
    window.localStorage.setItem("medrag-nexus-resource-panel", "open");
  }, []);

  const closeResourcePanel = useCallback(() => {
    setResourcePanelOpen(false);
    window.localStorage.setItem("medrag-nexus-resource-panel", "closed");
  }, []);

  const shellHeaderAction = useMemo(() => ({
    label: `已入库内容 ${resourceCount}`,
    controls: "resource-sidebar",
    expanded: resourcePanelOpen,
    onClick: resourcePanelOpen ? closeResourcePanel : openResourcePanel,
  }), [closeResourcePanel, openResourcePanel, resourceCount, resourcePanelOpen]);
  useShellHeaderAction(shellHeaderAction);

  const loadWorkspaceOptions = useCallback(async () => {
    try {
      await refreshWorkspaces();
    } catch (error) {
      setNotice({ kind: "error", title: "无法读取知识库", subtitle: describeError(error) });
    }
  }, [refreshWorkspaces, setNotice]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadWorkspaceOptions(), 0);
    return () => window.clearTimeout(timer);
  }, [loadWorkspaceOptions]);

  useEffect(() => {
    if (workspacePendingCreation) return;
    const timer = window.setTimeout(() => {
      setWorkspaceChoice(workspace.workspaceId);
      setWorkspaceConfirmed(Boolean(workspace.workspaceId));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [setWorkspaceChoice, setWorkspaceConfirmed, workspace.workspaceId, workspacePendingCreation]);

  useEffect(() => {
    const activeUserId = workspace.workspaceId ? workspace.userId : "";
    const selectedIsAvailable = workspaceUsers.some((item) => item.user_id === selectedKnowledgeUserId);
    const defaultUserId = activeUserId
      || (selectedIsAvailable ? selectedKnowledgeUserId : "")
      || workspaceUsers[0]?.user_id
      || "";
    if (defaultUserId !== selectedKnowledgeUserId) setSelectedKnowledgeUserId(defaultUserId);
  }, [selectedKnowledgeUserId, workspace.userId, workspace.workspaceId, workspaceUsers]);

  const loadFiles = useCallback(async () => {
    if (!workspace.workspaceId || workspacePendingCreation) {
      setFiles([]);
      setStrings([]);
      setStats(null);
      setListError(null);
      return;
    }
    setLoading(true);
    setListError(null);
    try {
      const response = await listWebUiFiles(workspace.workspaceId, true);
      setFiles(response.files);
      setStrings(response.strings);
      setStats(response.stats);
    } catch (error) {
      setListError(describeError(error));
    } finally {
      setLoading(false);
    }
  }, [workspace.workspaceId, workspacePendingCreation]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadFiles(), 0);
    return () => window.clearTimeout(timer);
  }, [loadFiles]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const saved = window.localStorage.getItem("medrag-nexus-resource-panel");
      if (saved === "open") setResourcePanelOpen(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!resourcePanelOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeResourcePanel();
    };
    const compactViewport = window.matchMedia("(max-width: 64rem)");
    const previousOverflow = document.body.style.overflow;
    if (compactViewport.matches) document.body.style.overflow = "hidden";
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [closeResourcePanel, resourcePanelOpen]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 5000);
    return () => window.clearTimeout(timer);
  }, [notice, setNotice]);

  useEffect(() => {
    const taskId = activeTask?.taskId;
    if (!taskId || activeTask.status === "succeeded" || activeTask.status === "failed") return;
    const taskLabel = activeTask.label;
    let stopped = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const task = await getWebUiTask(taskId);
        if (stopped) return;
        setActiveTask((current) => current?.taskId === taskId ? {
          ...current,
          status: task.status,
          stage: task.stage,
          percent: task.progress.percent,
        } : current);
        if (task.status === "succeeded") {
          const deleting = taskLabel.startsWith("删除 ");
          setSubmitting(false);
          setWorkspacePendingCreation(false);
          setContent("");
          setUploadKey((current) => current + 1);
          setNotice({
            kind: "success",
            title: deleting ? "删除完成" : "入库完成",
            subtitle: deleting ? "内容已经从当前知识库移除。" : "内容已经写入当前知识库。",
          });
          void loadWorkspaceOptions();
          void loadFiles();
          return;
        }
        if (task.status === "failed") {
          setSubmitting(false);
          const message = typeof task.error?.message === "string" ? task.error.message : "处理失败，请检查文件或稍后重试。";
          setNotice({ kind: "error", title: taskLabel.startsWith("删除 ") ? "删除失败" : "入库失败", subtitle: message });
          return;
        }
        timer = window.setTimeout(() => void poll(), 700);
      } catch (error) {
        if (stopped) return;
        setSubmitting(false);
        setNotice({ kind: "error", title: "无法读取进度", subtitle: describeError(error) });
      }
    };

    void poll();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [
    activeTask?.label,
    activeTask?.taskId,
    activeTask?.status,
    loadFiles,
    loadWorkspaceOptions,
    setActiveTask,
    setContent,
    setNotice,
    setSubmitting,
    setUploadKey,
    setWorkspacePendingCreation,
    workspace.userId,
  ]);

  useEffect(() => {
    if (
      !persistenceReady ||
      !needsUploadRecovery ||
      !workspace.userId ||
      !workspace.workspaceId ||
      !workspace.workspaceName ||
      !workspaceCapabilities?.can_add_file
    ) return;
    setNeedsUploadRecovery(false);
    const restoredUploads = fileUploads;

    void (async () => {
      setSubmitting(true);
      const storedFiles = await readUploadFiles(uploadPersistenceKey).catch(() => []);
      const filesById = new Map(storedFiles.map((item) => [item.localId, item.file]));
      let succeededCount = restoredUploads.filter((upload) => upload.status === "succeeded").length;
      let failedCount = restoredUploads.filter((upload) => upload.status === "failed").length;
      let cancelledCount = restoredUploads.filter((upload) => upload.status === "cancelled").length;

      for (const upload of restoredUploads) {
        if (upload.status === "succeeded" || upload.status === "failed" || upload.status === "cancelled") {
          await removeUploadFile(uploadPersistenceKey, upload.localId).catch(() => undefined);
          continue;
        }

        let taskId = upload.taskId;
        try {
          if (!taskId) {
            const file = filesById.get(upload.localId);
            if (!file) throw new Error("刷新前尚未提交，原文件不可用，请重新选择该文件。");
            setFileUploads((current) => current.map((item) => item.localId === upload.localId ? {
              ...item,
              status: "submitting",
              stage: "submitting",
              error: undefined,
            } : item));
            const form = new FormData();
            form.set("type", "file");
            form.set("file", file);
            try {
              const task = await addWebUiResource(workspace.workspaceId, form);
              taskId = task.task_id;
            } catch (error) {
              const existingTaskId = extractActiveTaskId(error);
              if (!existingTaskId) throw error;
              taskId = existingTaskId;
            }
            setFileUploads((current) => current.map((item) => item.localId === upload.localId ? {
              ...item,
              taskId,
              status: "queued",
              stage: "queued",
            } : item));
          }

          if (!taskId) throw new Error("没有可恢复的任务 ID，请重新选择该文件。");
          const outcome = await waitForUploadTask(taskId, upload.localId, setFileUploads);
          if (outcome === "succeeded") {
            succeededCount += 1;
            setWorkspacePendingCreation(false);
          } else if (outcome === "cancelled") {
            cancelledCount += 1;
          } else {
            failedCount += 1;
          }
        } catch (error) {
          failedCount += 1;
          setFileUploads((current) => current.map((item) => item.localId === upload.localId ? {
            ...item,
            status: "failed",
            error: describeError(error),
          } : item));
        } finally {
          await removeUploadFile(uploadPersistenceKey, upload.localId).catch(() => undefined);
        }
      }

      setSubmitting(false);
      setUploadKey((current) => current + 1);
      await Promise.all([loadWorkspaceOptions(), loadFiles()]);
      setNotice({
        kind: failedCount ? "warning" : "success",
        title: failedCount ? "上传任务已恢复，部分文件失败" : "上传任务已恢复并完成",
        subtitle: `${succeededCount} 个成功${failedCount ? `，${failedCount} 个失败` : ""}${cancelledCount ? `，${cancelledCount} 个已取消` : ""}。`,
      });
    })();
  }, [
    fileUploads,
    loadFiles,
    loadWorkspaceOptions,
    needsUploadRecovery,
    persistenceReady,
    setFileUploads,
    setNeedsUploadRecovery,
    setNotice,
    setSubmitting,
    setUploadKey,
    setWorkspacePendingCreation,
    workspace.userId,
    workspace.workspaceId,
    workspace.workspaceName,
    workspaceCapabilities?.can_add_file,
    uploadPersistenceKey,
  ]);

  function selectWorkspace(choice: string, preferredUserId?: string) {
    setWorkspaceChoice(choice);
    setNotice(null);
    setActiveTask(null);
    setFileUploads([]);
    if (uploadPersistenceKey) void clearUploadFiles(uploadPersistenceKey).catch(() => undefined);

    if (choice === "__new__") {
      setNewWorkspaceName("");
      setPendingWorkspaceId("");
      const defaultUser = preferredUserId
        ? creatableUsers.find((item) => item.user_id === preferredUserId)
        : preferredCreatableUser ?? creatableUsers[0];
      setNewWorkspaceUserId(defaultUser?.user_id ?? "");
      setReadMinLevel(0);
      setCudMinLevel(0);
      setWorkspacePendingCreation(true);
      setWorkspaceConfirmed(false);
      setWorkspace({ userId: workspace.userId, workspaceId: "", workspaceName: "" });
      return;
    }

    const selected = workspaceOptions.find((item) => item.workspace_id === choice);
    setNewWorkspaceName("");
    setPendingWorkspaceId("");
    setWorkspacePendingCreation(false);
    setWorkspaceConfirmed(Boolean(selected));
    if (selected) activateWorkspace(selected);
    else setWorkspace({ userId: preferredUserId ?? workspace.userId, workspaceId: "", workspaceName: "" });
  }

  function selectKnowledgeUser(userId: string) {
    setSelectedKnowledgeUserId(userId);
    setWorkspaceAction(null);
    setWorkspaceActionValue("");
    setNewKnowledgeUserOpen(false);
    selectWorkspace("", userId);
  }

  async function createKnowledgeUser() {
    const normalizedName = newKnowledgeUserName.replace(/\s+/g, " ").trim();
    if (!normalizedName || !can("webui.user.create")) return;
    setWorkspaceActionBusy(true);
    setNotice(null);
    try {
      const created = await registerKnowledgeDomain({ user_name: normalizedName });
      await refreshWorkspaces();
      setSelectedKnowledgeUserId(created.user_id);
      setWorkspace({ userId: created.user_id, workspaceId: "", workspaceName: "" });
      setWorkspaceChoice("");
      setWorkspaceConfirmed(false);
      setNewKnowledgeUserName("");
      setNewKnowledgeUserOpen(false);
      setNotice({
        kind: "success",
        title: "知识域已创建",
        subtitle: "现在可以在该知识域中新建一个或多个知识库。",
      });
    } catch (error) {
      setNotice({ kind: "error", title: "新建知识域失败", subtitle: describeError(error) });
    } finally {
      setWorkspaceActionBusy(false);
    }
  }

  function updateNewWorkspaceName(value: string) {
    setNewWorkspaceName(value);
    const normalizedName = value.replace(/\s+/g, " ").trim();
    setWorkspaceConfirmed(false);
    setWorkspace({ userId: workspace.userId, workspaceId: "", workspaceName: "" });
    if (!normalizedName) {
      setNotice(null);
      return;
    }
    if (workspaceOptions.some(
      (item) => item.user_id === newWorkspaceUserId && item.workspace_name === normalizedName,
    )) {
      setNotice({ kind: "error", title: "名称已存在", subtitle: "请直接选择同名知识库，或换一个名称。" });
      return;
    }
    setNotice(null);
    setWorkspacePendingCreation(true);
  }

  async function confirmNewWorkspace() {
    const normalizedName = newWorkspaceName.replace(/\s+/g, " ").trim();
    if (!normalizedName) {
      setNotice({ kind: "error", title: "请输入知识库名称", subtitle: "填写名称后再确认新建。" });
      return;
    }
    if (workspaceOptions.some(
      (item) => item.user_id === newWorkspaceUserId && item.workspace_name === normalizedName,
    )) {
      setNotice({ kind: "error", title: "名称已存在", subtitle: "请直接选择同名知识库，或换一个名称。" });
      return;
    }
    if (!newWorkspaceUserId) {
      setNotice({ kind: "error", title: "无法新建知识库", subtitle: "当前账号没有可新建知识库的用户范围。" });
      return;
    }
    setWorkspaceActionBusy(true);
    setNotice(null);
    try {
      const created = await registerKnowledgeBase({
        user_id: newWorkspaceUserId,
        workspace_name: normalizedName,
        read_min_level: readMinLevel,
        cud_min_level: cudMinLevel,
      });
      await refreshWorkspaces();
      activateWorkspace(created);
      setWorkspaceChoice(created.workspace_id);
      setWorkspacePendingCreation(false);
      setWorkspaceConfirmed(true);
      setPendingWorkspaceId("");
      setNotice({ kind: "success", title: "知识库已创建", subtitle: "现在可以向知识库添加内容。" });
    } catch (error) {
      setNotice({ kind: "error", title: "新建失败", subtitle: describeError(error) });
    } finally {
      setWorkspaceActionBusy(false);
    }
  }

  async function submitKnowledge(kind: "file" | "str", selectedFile?: File) {
    const canAddSelectedType = kind === "file"
      ? workspaceCapabilities?.can_add_file
      : workspaceCapabilities?.can_add_text;
    if (!canAddSelectedType) {
      setNotice({ kind: "error", title: "没有添加权限", subtitle: "当前知识库对你是只读的。" });
      return;
    }
    if (!workspaceConfirmed || !workspace.workspaceId || !workspace.workspaceName) {
      setNotice({ kind: "error", title: "请选择知识库", subtitle: "选择目标知识库后再添加内容。" });
      return;
    }
    if (kind === "file" && !selectedFile) return;
    if (kind === "str" && !content.trim()) {
      setNotice({ kind: "error", title: "文本为空", subtitle: "请输入需要入库的文本。" });
      return;
    }
    setSubmitting(true);
    setNotice(null);
    setActiveTask(null);
    try {
      const form = new FormData();
      form.set("type", kind);
      if (kind === "file" && selectedFile) form.set("file", selectedFile);
      if (kind === "str") form.set("content", content);
      const task = await addWebUiResource(workspace.workspaceId, form);
      setActiveTask({
        taskId: task.task_id,
        label: kind === "file" ? selectedFile?.name ?? "文件" : "文本内容",
        status: "queued",
        stage: "queued",
        percent: 0,
      });
    } catch (error) {
      setSubmitting(false);
      setNotice({ kind: "error", title: "上传失败", subtitle: describeError(error) });
    }
  }

  async function submitFiles(selectedFiles: File[]) {
    if (!workspaceCapabilities?.can_add_file) {
      setNotice({ kind: "error", title: "没有添加权限", subtitle: "当前知识库对你是只读的。" });
      return;
    }
    if (!workspaceConfirmed || !workspace.workspaceId || !workspace.workspaceName) {
      setNotice({ kind: "error", title: "请选择知识库", subtitle: "选择目标知识库后再上传文件。" });
      return;
    }
    if (selectedFiles.length === 0) return;
    if (selectedFiles.length > 5) {
      setNotice({ kind: "warning", title: "文件数量过多", subtitle: "一次最多上传 5 个文件，请重新选择。" });
      setUploadKey((current) => current + 1);
      return;
    }

    const uploads = selectedFiles.map<FileUploadTask>((file) => ({
      localId: generateUUID(),
      fileName: file.name,
      status: "waiting",
      stage: "waiting",
      percent: 0,
    }));
    await saveUploadFiles(uploadPersistenceKey, uploads.map((upload, index) => ({
      localId: upload.localId,
      file: selectedFiles[index],
    }))).catch(() => undefined);
    setSubmitting(true);
    setNotice(null);
    setActiveTask(null);
    setFileUploads(uploads);

    let succeededCount = 0;
    let failedCount = 0;
    let cancelledCount = 0;

    for (const [index, file] of selectedFiles.entries()) {
      const upload = uploads[index];
      if (cancelledUploadIds.current.has(upload.localId)) {
        cancelledCount += 1;
        await removeUploadFile(uploadPersistenceKey, upload.localId).catch(() => undefined);
        continue;
      }
      setFileUploads((current) => current.map((item) => item.localId === upload.localId ? {
        ...item,
        status: "submitting",
        stage: "submitting",
      } : item));
      try {
        const form = new FormData();
        form.set("type", "file");
        form.set("file", file);
        let taskId: string;
        try {
          const task = await addWebUiResource(workspace.workspaceId, form);
          taskId = task.task_id;
        } catch (error) {
          const existingTaskId = extractActiveTaskId(error);
          if (!existingTaskId) throw error;
          taskId = existingTaskId;
        }
        setFileUploads((current) => current.map((item) => item.localId === upload.localId ? {
          ...item,
          taskId,
          status: "queued",
          stage: "queued",
        } : item));

        if (cancelledUploadIds.current.has(upload.localId)) {
          await cancelWebUiTask(taskId);
          cancelledCount += 1;
          continue;
        }
        const outcome = await waitForUploadTask(
          taskId,
          upload.localId,
          setFileUploads,
          () => cancelledUploadIds.current.has(upload.localId),
        );
        if (outcome === "succeeded") {
          succeededCount += 1;
          setWorkspacePendingCreation(false);
        } else if (outcome === "cancelled") {
          cancelledCount += 1;
        } else {
          failedCount += 1;
        }
      } catch (error) {
        failedCount += 1;
        setFileUploads((current) => current.map((item) => item.localId === upload.localId ? {
          ...item,
          status: "failed",
          error: describeError(error),
        } : item));
      } finally {
        await removeUploadFile(uploadPersistenceKey, upload.localId).catch(() => undefined);
      }
    }

    await clearUploadFiles(uploadPersistenceKey).catch(() => undefined);
    setSubmitting(false);
    setUploadKey((current) => current + 1);
    await Promise.all([loadWorkspaceOptions(), loadFiles()]);
    setNotice({
      kind: failedCount ? "warning" : "success",
      title: failedCount ? "批量上传完成，部分文件失败" : cancelledCount ? "上传任务已取消" : "批量上传完成",
      subtitle: `${succeededCount} 个成功${failedCount ? `，${failedCount} 个失败` : ""}${cancelledCount ? `，${cancelledCount} 个已取消` : ""}。`,
    });
  }

  async function handleCancelUpload(upload: FileUploadTask) {
    const active = ["waiting", "submitting", "queued", "running"].includes(upload.status);
    if (!active) {
      setFileUploads((current) => current.filter((item) => item.localId !== upload.localId));
      await removeUploadFile(uploadPersistenceKey, upload.localId).catch(() => undefined);
      return;
    }
    if (!window.confirm(`确认取消“${upload.fileName}”的处理任务？`)) return;

    cancelledUploadIds.current.add(upload.localId);
    setCancellingUploadIds((current) => [...new Set([...current, upload.localId])]);
    setFileUploads((current) => current.map((item) => item.localId === upload.localId ? {
      ...item,
      status: "cancelled",
      stage: "cancelled",
      error: "已取消处理，临时文件正在清理。",
    } : item));
    try {
      if (upload.taskId) await cancelWebUiTask(upload.taskId);
      await removeUploadFile(uploadPersistenceKey, upload.localId).catch(() => undefined);
    } catch (error) {
      cancelledUploadIds.current.delete(upload.localId);
      setFileUploads((current) => current.map((item) => item.localId === upload.localId ? {
        ...item,
        status: "failed",
        stage: "cancel_failed",
        error: `取消失败：${describeError(error)}`,
      } : item));
    } finally {
      setCancellingUploadIds((current) => current.filter((localId) => localId !== upload.localId));
    }
  }

  function uploadSelectedFiles(selectedFiles: File[]) {
    const invalidType = selectedFiles.filter((file) => !ACCEPTED_FILES.some(
      (extension) => file.name.toLowerCase().endsWith(extension),
    ));
    const oversized = selectedFiles.filter((file) => file.size > MAX_FILE_SIZE_BYTES);
    if (invalidType.length || oversized.length) {
      const details = [
        invalidType.length ? `不支持的格式：${invalidType.map((file) => file.name).join("、")}` : "",
        oversized.length ? `超过 50 MiB：${oversized.map((file) => file.name).join("、")}` : "",
      ].filter(Boolean);
      setNotice({ kind: "error", title: "文件无法上传", subtitle: details.join("；") });
      setUploadKey((current) => current + 1);
      return;
    }
    void submitFiles(selectedFiles);
  }

  async function handleDelete(item: FileListItem) {
    if (!workspaceCapabilities?.can_delete_file) return;
    if (!workspace.workspaceId || !window.confirm(`确认删除 ${item.file_name}？此操作无法恢复。`)) return;
    try {
      const task = await deleteWebUiFile(workspace.workspaceId, item.file_id);
      setSubmitting(true);
      setActiveTask({
        taskId: task.task_id,
        label: `删除 ${item.file_name}`,
        status: "queued",
        stage: "queued",
        percent: 0,
      });
    } catch (error) {
      setNotice({ kind: "error", title: "删除失败", subtitle: describeError(error) });
    }
  }

  async function handleDeleteString(item: StringListItem) {
    if (!workspaceCapabilities?.can_delete_text) return;
    const preview = compactText(item.content ?? "这条文本");
    if (!workspace.workspaceId || !window.confirm(`确认删除“${preview}”？此操作无法恢复。`)) return;
    try {
      const task = await deleteWebUiString(workspace.workspaceId, item.content_hash);
      setSubmitting(true);
      setActiveTask({
        taskId: task.task_id,
        label: `删除 文本 ${preview}`,
        status: "queued",
        stage: "queued",
        percent: 0,
      });
    } catch (error) {
      setNotice({ kind: "error", title: "删除失败", subtitle: describeError(error) });
    }
  }

  async function handleRenameWorkspace() {
    const nextName = workspaceActionValue.replace(/\s+/g, " ").trim();
    if (!workspace.workspaceId || !workspaceCapabilities?.can_rename || !nextName) return;
    setWorkspaceActionBusy(true);
    setNotice(null);
    try {
      const updated = await renameWebUiWorkspace(workspace.workspaceId, nextName);
      await refreshWorkspaces();
      activateWorkspace(updated);
      setWorkspaceChoice(updated.workspace_id);
      setWorkspaceAction(null);
      setWorkspaceActionValue("");
      setNotice({ kind: "success", title: "知识库已改名", subtitle: `新名称：${updated.workspace_name}` });
    } catch (error) {
      setNotice({ kind: "error", title: "改名失败", subtitle: describeError(error) });
    } finally {
      setWorkspaceActionBusy(false);
    }
  }

  async function handleWorkspacePolicy() {
    if (!workspace.workspaceId || !workspaceCapabilities?.can_manage_policy) return;
    setWorkspaceActionBusy(true);
    setNotice(null);
    try {
      const updated = await updateWebUiWorkspacePolicy(workspace.workspaceId, {
        read_min_level: policyReadLevel,
        cud_min_level: policyCudLevel,
      });
      await refreshWorkspaces();
      activateWorkspace(updated);
      setWorkspaceAction(null);
      setNotice({
        kind: "success",
        title: "权限策略已更新",
        subtitle: `读取等级 ${updated.read_min_level}，增删等级 ${updated.cud_min_level}。`,
      });
    } catch (error) {
      setNotice({ kind: "error", title: "权限更新失败", subtitle: describeError(error) });
    } finally {
      setWorkspaceActionBusy(false);
    }
  }

  async function handleDeleteWorkspace() {
    if (
      !workspace.workspaceId ||
      !workspaceCapabilities?.can_delete_workspace ||
      workspaceActionValue !== workspace.workspaceName
    ) return;
    setWorkspaceActionBusy(true);
    setNotice(null);
    try {
      const result = await deleteWebUiWorkspace(workspace.workspaceId, workspace.workspaceName);
      setWorkspace({ userId: workspaceUsers[0]?.user_id ?? "", workspaceId: "", workspaceName: "" });
      setWorkspaceChoice("");
      setWorkspaceConfirmed(false);
      setWorkspacePendingCreation(false);
      setFiles([]);
      setStrings([]);
      setStats(null);
      setWorkspaceAction(null);
      setWorkspaceActionValue("");
      await refreshWorkspaces();
      setNotice(result.status === "cleanup_pending" ? {
        kind: "warning",
        title: "知识库已移除，后台仍在清理",
        subtitle: "该知识库不会重新出现在可访问列表中；文件、Elasticsearch 或 Milvus 的残留正在后台重试清理。",
      } : {
        kind: "success",
        title: "知识库已删除",
        subtitle: "知识库及其内容已从 SQLite、文件目录、Elasticsearch 和 Milvus 清理。",
      });
    } catch (error) {
      setNotice({ kind: "error", title: "删除知识库失败", subtitle: describeError(error) });
    } finally {
      setWorkspaceActionBusy(false);
    }
  }

  return (
    <div className="page documents-page">
      <div className="documents-layout" data-sidebar-open={resourcePanelOpen}>
        <main className="documents-primary">
          {notice ? (
            <div className="notice-stack">
              <InlineNotification
                kind={notice.kind}
                title={notice.title}
                subtitle={notice.subtitle}
                onCloseButtonClick={() => setNotice(null)}
              />
            </div>
          ) : null}

          <section className="panel workspace-setup" aria-labelledby="workspace-setup-title">
            <div className="section-heading workspace-setup-heading">
              <div>
                <h2 className="panel-heading" id="workspace-setup-title">目标知识库</h2>
              </div>
              <div className="workspace-heading-actions">
                {can("webui.user.create") ? (
                  <Button
                    size="sm"
                    kind="ghost"
                    renderIcon={Add}
                    disabled={submitting || workspaceActionBusy}
                    onClick={() => {
                      setNewKnowledgeUserOpen((current) => !current);
                      setNewKnowledgeUserName("");
                    }}
                  >新建知识域</Button>
                ) : null}
                {selectedCreatableUser ? (
                  <Button
                    size="sm"
                    renderIcon={DocumentAdd}
                    disabled={submitting || workspaceActionBusy}
                    onClick={() => selectWorkspace("__new__", selectedCreatableUser.user_id)}
                  >新建知识库</Button>
                ) : null}
                {workspaceCapabilities?.can_manage_policy && selectedWorkspace ? (
                  <Button
                    kind="ghost"
                    size="sm"
                    disabled={submitting || workspaceActionBusy}
                    onClick={() => {
                      setWorkspaceAction("policy");
                      setPolicyReadLevel(selectedWorkspace.read_min_level);
                      setPolicyCudLevel(selectedWorkspace.cud_min_level);
                    }}
                  >权限设置</Button>
                ) : null}
                {workspaceCapabilities?.can_rename ? (
                  <Button
                    kind="ghost"
                    size="sm"
                    disabled={submitting || workspaceActionBusy}
                    onClick={() => {
                      setWorkspaceAction("rename");
                      setWorkspaceActionValue(workspace.workspaceName);
                    }}
                  >重命名</Button>
                ) : null}
                {workspaceCapabilities?.can_delete_workspace ? (
                  <Button
                    kind="danger--ghost"
                    size="sm"
                    disabled={submitting || workspaceActionBusy}
                    onClick={() => {
                      setWorkspaceAction("delete");
                      setWorkspaceActionValue("");
                    }}
                  >删除知识库</Button>
                ) : null}
                <Button
                  kind="ghost"
                  size="sm"
                  renderIcon={Renew}
                  disabled={workspaceLoading}
                  onClick={() => void loadWorkspaceOptions()}
                >
                  刷新
                </Button>
              </div>
            </div>
            {newKnowledgeUserOpen ? (
              <div className="new-knowledge-user-confirm">
                <TextInput
                  id="new-knowledge-user-name"
                  labelText="知识域名称"
                  placeholder="例如：产品资料"
                  value={newKnowledgeUserName}
                  disabled={submitting || workspaceActionBusy}
                  onChange={(event) => setNewKnowledgeUserName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void createKnowledgeUser();
                  }}
                />
                <Button
                  type="button"
                  disabled={submitting || workspaceActionBusy || !newKnowledgeUserName.trim()}
                  onClick={() => void createKnowledgeUser()}
                >{workspaceActionBusy ? "正在创建" : "创建知识域"}</Button>
                <Button
                  type="button"
                  kind="ghost"
                  disabled={workspaceActionBusy}
                  onClick={() => setNewKnowledgeUserOpen(false)}
                >取消</Button>
              </div>
            ) : null}
            <div className="workspace-setup-fields">
              <Select
                id="document-knowledge-user-selector"
                labelText="知识域"
                value={selectedKnowledgeUserId}
                disabled={submitting || workspaceLoading}
                onChange={(event) => selectKnowledgeUser(event.target.value)}
              >
                <SelectItem value="" text={workspaceLoading ? "正在读取知识域" : "请选择知识域"} />
                {workspaceUsers.map((item) => (
                  <SelectItem
                    key={item.user_id}
                    value={item.user_id}
                    text={`${item.user_name}（${workspaceOptions.filter((workspaceItem) => workspaceItem.user_id === item.user_id).length} 个知识库）`}
                  />
                ))}
              </Select>
              <Select
                id="document-workspace-selector"
                labelText="知识库"
                value={workspaceChoice}
                disabled={submitting || !selectedKnowledgeUserId}
                onChange={(event) => selectWorkspace(event.target.value, selectedKnowledgeUserId)}
              >
                <SelectItem
                  value=""
                  text={workspaceLoading
                    ? "正在读取知识库"
                    : selectedKnowledgeUserId
                      ? knowledgeUserWorkspaces.length ? "请选择知识库" : "当前知识域暂无知识库"
                      : "请先选择知识域"}
                />
                {knowledgeUserWorkspaces.map((item) => (
                  <SelectItem
                    key={item.workspace_id}
                    value={item.workspace_id}
                    text={`${item.workspace_name}（${item.resource_count} 项内容）`}
                  />
                ))}
                {selectedCreatableUser ? <SelectItem value="__new__" text="在当前知识域新建知识库" /> : null}
              </Select>
              {workspaceChoice === "__new__" ? (
                <div className="new-workspace-confirm">
                  <TextInput
                    id="new-workspace-user"
                    labelText="归属知识域"
                    value={selectedKnowledgeUser?.user_name ?? ""}
                    readOnly
                  />
                  <TextInput
                    id="new-workspace-name"
                    labelText="知识库名称"
                    placeholder="例如：产品知识库"
                    value={newWorkspaceName}
                    disabled={submitting || workspaceActionBusy}
                    onChange={(event) => updateNewWorkspaceName(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !workspaceConfirmed) void confirmNewWorkspace();
                    }}
                  />
                  <Select
                    id="new-workspace-read-level"
                    labelText="最低读取等级"
                    value={String(readMinLevel)}
                    disabled={workspaceActionBusy}
                    onChange={(event) => setReadMinLevel(Number(event.target.value))}
                  >
                    {availableLevels.map((level) => (
                      <SelectItem key={level} value={String(level)} text={String(level)} />
                    ))}
                  </Select>
                  <Select
                    id="new-workspace-cud-level"
                    labelText="最低增删等级"
                    value={String(cudMinLevel)}
                    disabled={workspaceActionBusy}
                    onChange={(event) => setCudMinLevel(Number(event.target.value))}
                  >
                    {availableLevels.map((level) => (
                      <SelectItem key={level} value={String(level)} text={String(level)} />
                    ))}
                  </Select>
                  <Button
                    type="button"
                    size="md"
                    disabled={submitting || workspaceActionBusy || workspaceConfirmed || !newWorkspaceName.trim() || !newWorkspaceUserId}
                    onClick={() => void confirmNewWorkspace()}
                  >
                    {workspaceActionBusy ? "正在创建" : workspaceConfirmed ? "已创建" : "新建"}
                  </Button>
                </div>
              ) : null}
            </div>
            {workspaceAction && (workspaceAction !== "policy" || workspaceCapabilities?.can_manage_policy) ? (
              <div className="workspace-maintenance-form" data-mode={workspaceAction}>
                {workspaceAction === "policy" ? (
                  <>
                    <PolicyBindingsEditor
                      resourceType="workspace"
                      resourceId={workspace.workspaceId}
                      actions={WORKSPACE_POLICY_ACTIONS}
                      canListAccounts={can("webui.account.manage")}
                      disabled={workspaceActionBusy}
                      onSaved={() => void refreshWorkspaces()}
                    />
                    <details className="workspace-policy-advanced">
                      <summary><span>高级设置：成员等级门槛</span><small>通常无需修改</small></summary>
                      <div className="workspace-policy-advanced-body">
                        <p>只有达到等级门槛且出现在允许名单中的成员才能访问。</p>
                        <Select
                          id="workspace-policy-read-level"
                          labelText="查看知识库的最低等级"
                          value={String(policyReadLevel)}
                          disabled={workspaceActionBusy}
                          onChange={(event) => setPolicyReadLevel(Number(event.target.value))}
                        >
                          {availableLevels.map((level) => (
                            <SelectItem key={level} value={String(level)} text={String(level)} />
                          ))}
                        </Select>
                        <Select
                          id="workspace-policy-cud-level"
                          labelText="维护知识库的最低等级"
                          value={String(policyCudLevel)}
                          disabled={workspaceActionBusy}
                          onChange={(event) => setPolicyCudLevel(Number(event.target.value))}
                        >
                          {availableLevels.map((level) => (
                            <SelectItem key={level} value={String(level)} text={String(level)} />
                          ))}
                        </Select>
                        <Button type="button" disabled={workspaceActionBusy} onClick={() => void handleWorkspacePolicy()}>{workspaceActionBusy ? "正在保存" : "保存等级门槛"}</Button>
                      </div>
                    </details>
                  </>
                ) : (
                  <TextInput
                    id="workspace-maintenance-value"
                    labelText={workspaceAction === "rename" ? "新名称" : `输入“${workspace.workspaceName}”确认整库删除`}
                    value={workspaceActionValue}
                    disabled={workspaceActionBusy}
                    onChange={(event) => setWorkspaceActionValue(event.target.value)}
                  />
                )}
                {workspaceAction !== "policy" ? <Button
                  type="button"
                  kind={workspaceAction === "delete" ? "danger" : "primary"}
                  disabled={
                    workspaceActionBusy ||
                    !workspaceActionValue.trim() ||
                    (workspaceAction === "delete" && workspaceActionValue !== workspace.workspaceName)
                  }
                  onClick={() => void (
                    workspaceAction === "rename"
                      ? handleRenameWorkspace()
                      : handleDeleteWorkspace()
                  )}
                >
                  {workspaceActionBusy
                    ? "正在处理"
                    : workspaceAction === "rename"
                      ? "确认改名"
                      : "确认整库删除"}
                </Button> : null}
                <Button
                  type="button"
                  kind="ghost"
                  disabled={workspaceActionBusy}
                  onClick={() => {
                    setWorkspaceAction(null);
                    setWorkspaceActionValue("");
                  }}
                >取消</Button>
              </div>
            ) : null}
          </section>

          {canAddContent ? (
            <section className="panel form-panel ingestion-panel" id="add">
              <div className="section-heading">
                <div>
                  <h2 className="panel-heading">添加内容</h2>
                </div>
              </div>

              <div className="source-tabs" role="tablist" aria-label="内容类型">
                <button
                  type="button"
                  role="tab"
                  aria-selected={sourceType === "file"}
                  data-selected={sourceType === "file"}
                  disabled={submitting || !canAddFile}
                  onClick={() => setSourceType("file")}
                >
                  <DocumentAdd size={18} />
                  <strong>文件</strong>
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={sourceType === "str"}
                  data-selected={sourceType === "str"}
                  disabled={submitting || !canAddText}
                  onClick={() => setSourceType("str")}
                >
                  <TextCreation size={18} />
                  <strong>文本</strong>
                </button>
              </div>

              <div className="source-panel" role="tabpanel">
                {sourceType === "file" ? (
                  <div className="upload-zone" data-disabled={submitting || !canAddFile}>
                    <FileUploaderDropContainer
                      key={uploadKey}
                      id={`knowledge-file-upload-${uploadKey}`}
                      accept={ACCEPTED_FILES}
                      disabled={submitting || !canAddFile}
                      labelText="选择要上传的文件"
                      maxFileSize={MAX_FILE_SIZE_BYTES}
                      multiple
                      onAddFiles={(_event, { addedFiles }) => {
                        if (addedFiles.length) uploadSelectedFiles(addedFiles);
                      }}
                    />
                    <div className="upload-zone-content">
                      <div className="upload-zone-icon"><Upload size={30} /></div>
                      <h3>{submitting ? "正在处理上传任务" : "将文件拖到这里"}</h3>
                      <p>{submitting ? "完成后即可继续添加文件" : "也可以点击此区域选择文件，选择后会自动上传"}</p>
                      <div className="upload-zone-formats" aria-label="支持的文件格式">
                        <span>PDF</span><span>TXT</span><span>DOCX</span>
                      </div>
                      <p className="upload-zone-help">单个文件不超过 50 MiB · 每次最多选择 5 个</p>
                    </div>
                  </div>
                ) : (
                  <div className="text-upload-panel">
                    <TextArea
                      id="string-content"
                      labelText="文本内容"
                      placeholder="粘贴或输入要加入知识库的内容"
                      rows={10}
                      value={content}
                      disabled={submitting || !canAddText}
                      onChange={(event) => setContent(event.target.value)}
                    />
                    <div className="form-actions form-actions-end">
                      <Button
                        type="button"
                        disabled={submitting || !canAddText || !content.trim()}
                        renderIcon={Upload}
                        onClick={() => void submitKnowledge("str")}
                      >
                        上传文本
                      </Button>
                    </div>
                  </div>
                )}
              </div>

              {activeTask ? (
                <div className="upload-progress" aria-live="polite">
                  <div className="upload-progress-heading">
                    <strong>{activeTask.label}</strong>
                    <div className="upload-progress-actions">
                      <span>{Math.round(activeTask.percent)}%</span>
                      {(activeTask.status === "succeeded" || activeTask.status === "failed") && (
                        <button
                          type="button"
                          className="close-button"
                          onClick={() => setActiveTask(null)}
                          aria-label="关闭任务进度"
                        >
                          ✕
                        </button>
                      )}
                    </div>
                  </div>
                  <ProgressBar
                    label="处理进度"
                    hideLabel
                    max={100}
                    value={activeTask.percent}
                    status={activeTask.status === "failed" ? "error" : activeTask.status === "succeeded" ? "finished" : "active"}
                    helperText={taskStageLabel(activeTask.stage, activeTask.status)}
                  />
                  {activeTask.taskId && (
                    <p className="upload-task-id">
                      <span>Task ID</span>
                      <code>{activeTask.taskId}</code>
                    </p>
                  )}
                </div>
              ) : null}

              {fileUploads.length ? (
                <div className="upload-batch" aria-live="polite" aria-label="批量上传进度">
                  <div className="upload-batch-heading">
                    <strong>文件处理进度</strong>
                    <div className="upload-batch-heading-actions">
                      <span>{fileUploads.filter((upload) => upload.status === "succeeded").length}/{fileUploads.length} 完成</span>
                      {!submitting ? (
                        <button
                          type="button"
                          className="upload-another-button"
                          onClick={() => {
                            setFileUploads([]);
                            setUploadKey((current) => current + 1);
                            void clearUploadFiles(uploadPersistenceKey).catch(() => undefined);
                          }}
                        >
                          继续上传
                        </button>
                      ) : null}
                    </div>
                  </div>
                  <div className="upload-batch-list">
                    {fileUploads.map((upload) => (
                      <div className="upload-batch-item" data-status={upload.status} key={upload.localId}>
                        <div className="upload-progress-heading">
                          <strong>{upload.fileName}</strong>
                          <div className="upload-progress-actions">
                            <span>
                              {upload.status === "waiting"
                                ? "等待"
                                : upload.status === "submitting"
                                  ? "提交中"
                                  : upload.status === "cancelled"
                                    ? "已取消"
                                    : `${Math.round(upload.percent)}%`}
                            </span>
                            <button
                              type="button"
                              className="upload-cancel-button"
                              disabled={cancellingUploadIds.includes(upload.localId)}
                              onClick={() => void handleCancelUpload(upload)}
                              aria-label={(["waiting", "submitting", "queued", "running"] as string[]).includes(upload.status)
                                ? `取消 ${upload.fileName} 的处理任务`
                                : `移除 ${upload.fileName} 的任务记录`}
                            >
                              <TrashCan size={14} aria-hidden="true" />
                              {(["waiting", "submitting", "queued", "running"] as string[]).includes(upload.status)
                                ? "取消任务"
                                : "移除"}
                            </button>
                          </div>
                        </div>
                        <ProgressBar
                          label={`${upload.fileName} 处理进度`}
                          hideLabel
                          max={100}
                          value={upload.percent}
                          status={upload.status === "failed" || upload.status === "cancelled" ? "error" : upload.status === "succeeded" ? "finished" : "active"}
                          helperText={upload.error ?? taskStageLabel(upload.stage, upload.status)}
                        />
                        <p className="upload-task-id">
                          <span>Task ID</span>
                          <code>
                            {upload.taskId
                              ?? (upload.status === "waiting"
                                ? "等待提交后生成"
                                : upload.status === "submitting"
                                  ? "正在生成"
                                  : "未创建任务")}
                          </code>
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </section>
          ) : workspaceConfirmed ? (
            <div className="workspace-prompt">
              <EmptyState title="当前知识库为只读" description="你可以查看已入库内容，但没有 Workspace CUD 权限，文件拖拽、选择上传和文本添加均已关闭。" />
            </div>
          ) : (
            <div className="workspace-prompt">
              <EmptyState title="请选择知识库" description="选择后即可上传文件或粘贴文本。" />
            </div>
          )}
        </main>

        {resourcePanelOpen ? (
          <button
            type="button"
            className="resource-sidebar-backdrop"
            aria-label="关闭已入库内容"
            onClick={closeResourcePanel}
          />
        ) : null}

        <aside
          className="resource-sidebar"
          id="resource-sidebar"
          data-open={resourcePanelOpen}
          aria-label="已入库内容"
        >
          {resourcePanelOpen ? (
            <div className="resource-sidebar-panel">
              <header className="resource-sidebar-header">
                <div>
                  <div className="resource-sidebar-title-row">
                    <h2>已入库内容</h2>
                    <span className="resource-count">{resourceCount}</span>
                  </div>
                  <p>{workspace.workspaceName || "尚未选择知识库"}</p>
                </div>
                <button
                  type="button"
                  className="resource-sidebar-close"
                  aria-label="收起已入库内容"
                  onClick={closeResourcePanel}
                >
                  <ChevronRight className="resource-close-desktop" size={20} />
                  <Close className="resource-close-compact" size={20} />
                </button>
              </header>

              <div className="resource-sidebar-controls">
                <div className="resource-filter-tabs" role="tablist" aria-label="筛选已入库内容">
                  <button type="button" role="tab" aria-selected={resourceFilter === "all"} data-selected={resourceFilter === "all"} onClick={() => setResourceFilter("all")}>全部</button>
                  <button type="button" role="tab" aria-selected={resourceFilter === "file"} data-selected={resourceFilter === "file"} onClick={() => setResourceFilter("file")}>文件</button>
                  <button type="button" role="tab" aria-selected={resourceFilter === "str"} data-selected={resourceFilter === "str"} onClick={() => setResourceFilter("str")}>文本</button>
                </div>
                <Button
                  kind="ghost"
                  size="sm"
                  renderIcon={Renew}
                  disabled={loading || !workspace.workspaceId}
                  onClick={() => void loadFiles()}
                >
                  刷新
                </Button>
              </div>

              {stats ? (
                <p className="resource-sidebar-summary">
                  {stats.file_count} 个文件，{stats.str_count} 条文本，共 {formatBytes(stats.total_size_bytes)}
                </p>
              ) : null}

              <div className="resource-sidebar-body" aria-live="polite">
                {!workspace.workspaceId ? (
                  <EmptyState title="选择一个知识库" description="应用知识空间后即可查看已入库内容。" />
                ) : loading ? (
                  <LoadingState label="正在读取已入库内容" />
                ) : listError ? (
                  <InlineNotification kind="error" title="无法读取内容" subtitle={listError} lowContrast />
                ) : filteredResources.length === 0 ? (
                  <EmptyState
                    title={resourceFilter === "all" ? "暂无已入库内容" : resourceFilter === "file" ? "暂无文件" : "暂无文本"}
                    description="完成一次入库后，内容会显示在这里。"
                  />
                ) : (
                  <div className="resource-list">
                    {filteredResources.map((resource) => {
                      const item = resource.item;
                      const title = resource.kind === "file"
                        ? resource.item.file_name
                        : compactText(resource.item.content ?? "内容未加载");
                      return (
                        <article className="resource-item" key={resource.key}>
                          <div className="resource-item-heading">
                            <span className="resource-kind">{resource.kind === "file" ? "文件" : "文本"}</span>
                            <time dateTime={item.modified_at}>{formatDate(item.modified_at)}</time>
                          </div>
                          <h3>{title}</h3>
                          <p className="resource-item-meta">{formatBytes(item.size_bytes)}</p>
                          <div className="resource-item-actions">
                            <details className="resource-details">
                              <summary>详情</summary>
                              <dl>
                                <div>
                                  <dt>内容哈希</dt>
                                  <dd className="code-value">{item.content_hash}</dd>
                                </div>
                                <div>
                                  <dt>创建时间</dt>
                                  <dd>{formatDate(item.created_at)}</dd>
                                </div>
                              </dl>
                            </details>
                            {workspaceCapabilities?.can_delete_file && resource.kind === "file" ? (
                              <Button
                                kind="danger--ghost"
                                size="sm"
                                renderIcon={TrashCan}
                                onClick={() => void handleDelete(resource.item)}
                              >
                                删除
                              </Button>
                            ) : workspaceCapabilities?.can_delete_text && resource.kind === "str" ? (
                              <Button
                                kind="danger--ghost"
                                size="sm"
                                renderIcon={TrashCan}
                                onClick={() => void handleDeleteString(resource.item)}
                              >
                                删除
                              </Button>
                            ) : null}
                          </div>
                        </article>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}

function compactText(value: string): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > 72 ? `${compact.slice(0, 72)}…` : compact;
}

function extractActiveTaskId(error: unknown): string | null {
  if (error instanceof WebUiApiError) {
    const activeTaskId = error.details?.active_task_id;
    if (typeof activeTaskId === "string" && /^[0-9a-f]{32}$/i.test(activeTaskId)) return activeTaskId;
  }
  return describeError(error).match(/task[_\s-]*id\s*[:：]\s*([0-9a-f]{32}|[0-9a-f-]{36})/i)?.[1] ?? null;
}

type UploadOutcome = "succeeded" | "failed" | "cancelled";

async function waitForUploadTask(
  taskId: string,
  localId: string,
  setUploads: Dispatch<SetStateAction<FileUploadTask[]>>,
  shouldCancel: () => boolean = () => false,
): Promise<UploadOutcome> {
  while (true) {
    if (shouldCancel()) return "cancelled";
    try {
      const task = await getWebUiTask(taskId);
      const cancelled = task.status === "failed" && task.error?.code === "TASK_CANCELLED";
      const error = cancelled
        ? "任务已取消，临时文件已清理。"
        : task.status === "failed"
        ? (typeof task.error?.message === "string" ? task.error.message : "处理失败，请检查文件。")
        : undefined;
      setUploads((current) => current.map((upload) => upload.localId === localId ? {
        ...upload,
        status: cancelled ? "cancelled" : task.status,
        stage: task.stage,
        percent: task.progress.percent,
        error,
      } : upload));
      if (task.status === "succeeded") return "succeeded";
      if (task.status === "failed") return cancelled ? "cancelled" : "failed";
    } catch (error) {
      setUploads((current) => current.map((upload) => upload.localId === localId ? {
        ...upload,
        status: "failed",
        error: describeError(error),
      } : upload));
      return "failed";
    }
    await new Promise<void>((resolve) => window.setTimeout(resolve, 700));
  }
}

function taskStageLabel(stage: string, status: ActiveTask["status"] | FileUploadTask["status"]): string {
  if (status === "waiting") return "等待前一个文件处理完成";
  if (status === "submitting") return "正在提交任务";
  if (status === "queued") return "等待处理";
  if (status === "succeeded") return "处理完成";
  if (status === "failed") return "处理失败";
  if (status === "cancelled") return "任务已取消";
  const labels: Record<string, string> = {
    staging: "准备内容",
    parsing: "解析内容",
    chunking: "切分内容",
    embedding: "生成向量",
    indexing: "写入索引",
    metadata: "保存元数据",
    deleting: "正在删除",
    completed: "处理完成",
    cancelled: "任务已取消",
  };
  return labels[stage] ?? "正在处理";
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}
