"use client";

import {
  createContext,
  type Dispatch,
  type MutableRefObject,
  type ReactNode,
  type SetStateAction,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { useWorkspace } from "@/components/workspace";
import { readBrowserValue, writeBrowserValue } from "@/lib/browser-persistence";
import type { AgentInteraction } from "@/lib/agent-types";
import type { RetrievalResponse } from "@/lib/types";
import { generateUUID } from "@/lib/uuid";

const PAGE_MEMORY_VERSION = 1;

export interface Notice {
  kind: "success" | "error" | "warning";
  title: string;
  subtitle: string;
}

export interface ActiveTask {
  taskId: string;
  label: string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  percent: number;
}

export interface FileUploadTask {
  localId: string;
  fileName: string;
  taskId?: string;
  status: "waiting" | "submitting" | "queued" | "running" | "succeeded" | "failed" | "cancelled";
  stage: string;
  percent: number;
  error?: string;
}

export interface ChatSource {
  citation_id: number;
  workspace_id: string;
  workspace_name: string;
  source_type: "file" | "str";
  file_id?: string | null;
  file_name?: string | null;
  chunk_id: string;
  section?: string | null;
  page_number?: number | null;
  excerpt: string;
}

export interface VisibleMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  delivery?: "pending" | "complete" | "stopped" | "failed";
  sources?: ChatSource[];
  interactions?: AgentInteraction[];
}

export interface MemoryConversation {
  localId: string;
  remoteId: string | null;
  title: string;
  createdAt: number | null;
  messages: VisibleMessage[];
}

type Setter<T> = Dispatch<SetStateAction<T>>;

interface DocumentMemoryValue {
  persistenceReady: boolean;
  needsUploadRecovery: boolean;
  setNeedsUploadRecovery: Setter<boolean>;
  sourceType: "file" | "str";
  setSourceType: Setter<"file" | "str">;
  content: string;
  setContent: Setter<string>;
  workspaceChoice: string;
  setWorkspaceChoice: Setter<string>;
  newWorkspaceName: string;
  setNewWorkspaceName: Setter<string>;
  pendingWorkspaceId: string;
  setPendingWorkspaceId: Setter<string>;
  workspaceConfirmed: boolean;
  setWorkspaceConfirmed: Setter<boolean>;
  workspacePendingCreation: boolean;
  setWorkspacePendingCreation: Setter<boolean>;
  submitting: boolean;
  setSubmitting: Setter<boolean>;
  activeTask: ActiveTask | null;
  setActiveTask: Setter<ActiveTask | null>;
  fileUploads: FileUploadTask[];
  setFileUploads: Setter<FileUploadTask[]>;
  uploadKey: number;
  setUploadKey: Setter<number>;
  notice: Notice | null;
  setNotice: Setter<Notice | null>;
}

interface ChatMemoryValue {
  conversations: MemoryConversation[];
  setConversations: Setter<MemoryConversation[]>;
  activeId: string;
  setActiveId: Setter<string>;
  input: string;
  setInput: Setter<string>;
  busy: boolean;
  setBusy: Setter<boolean>;
  status: string;
  setStatus: Setter<string>;
  error: string | null;
  setError: Setter<string | null>;
  abortRef: MutableRefObject<AbortController | null>;
}

interface RetrievalMemoryValue {
  query: string;
  setQuery: Setter<string>;
  topK: number;
  setTopK: Setter<number>;
  result: RetrievalResponse | null;
  setResult: Setter<RetrievalResponse | null>;
  elapsed: number | null;
  setElapsed: Setter<number | null>;
  loading: boolean;
  setLoading: Setter<boolean>;
  error: string | null;
  setError: Setter<string | null>;
}

interface PersistedPageMemory {
  version: number;
  document: {
    sourceType: "file" | "str";
    content: string;
    workspaceChoice: string;
    newWorkspaceName: string;
    pendingWorkspaceId: string;
    workspaceConfirmed: boolean;
    workspacePendingCreation: boolean;
    submitting: boolean;
    activeTask: ActiveTask | null;
    fileUploads: FileUploadTask[];
    uploadKey: number;
  };
  retrieval: {
    query: string;
    topK: number;
    result: RetrievalResponse | null;
    elapsed: number | null;
  };
  chat?: {
    conversations: MemoryConversation[];
    activeId: string;
  };
}

const DocumentMemoryContext = createContext<DocumentMemoryValue | null>(null);
const ChatMemoryContext = createContext<ChatMemoryValue | null>(null);
const RetrievalMemoryContext = createContext<RetrievalMemoryValue | null>(null);

function sanitizeConversations(value: MemoryConversation[] | undefined): MemoryConversation[] {
  if (!Array.isArray(value)) return [];
  return value.filter((conversation) => (
    typeof conversation?.localId === "string"
    && Array.isArray(conversation.messages)
  )).map((conversation) => ({
    ...conversation,
    messages: conversation.messages.filter((message) => (
      typeof message?.id === "string"
      && (message.role === "user" || message.role === "assistant")
      && typeof message.content === "string"
    )).map((message): VisibleMessage => ({
      ...message,
      delivery: message.role === "assistant"
        ? (message.delivery === "failed" || message.delivery === "complete" ? message.delivery : "stopped")
        : undefined,
    })),
  })).slice(0, 50);
}

export function createConversation(localId = generateUUID()): MemoryConversation {
  return { localId, remoteId: null, title: "新对话", createdAt: null, messages: [] };
}

function pageMemoryKey(userId: string): string {
  return `page-memory-v${PAGE_MEMORY_VERSION}:${userId}`;
}

export function AppMemoryProvider({ children }: { children: ReactNode }) {
  const { workspace } = useWorkspace();
  const initialConversationId = useId();

  const [persistenceReady, setPersistenceReady] = useState(false);
  const [needsUploadRecovery, setNeedsUploadRecovery] = useState(false);
  const [sourceType, setSourceType] = useState<"file" | "str">("file");
  const [content, setContent] = useState("");
  const [workspaceChoice, setWorkspaceChoice] = useState(workspace.workspaceId);
  const [newWorkspaceName, setNewWorkspaceName] = useState("");
  const [pendingWorkspaceId, setPendingWorkspaceId] = useState("");
  const [workspaceConfirmed, setWorkspaceConfirmed] = useState(Boolean(workspace.workspaceId));
  const [workspacePendingCreation, setWorkspacePendingCreation] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [activeTask, setActiveTask] = useState<ActiveTask | null>(null);
  const [fileUploads, setFileUploads] = useState<FileUploadTask[]>([]);
  const [uploadKey, setUploadKey] = useState(0);
  const [notice, setNotice] = useState<Notice | null>(null);

  const [conversations, setConversations] = useState<MemoryConversation[]>(() => [createConversation(initialConversationId)]);
  const [activeId, setActiveId] = useState(initialConversationId);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [chatError, setChatError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(10);
  const [result, setResult] = useState<RetrievalResponse | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [retrievalLoading, setRetrievalLoading] = useState(false);
  const [retrievalError, setRetrievalError] = useState<string | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    let cancelled = false;
    const userId = workspace.userId;
    if (!userId) {
      const timer = window.setTimeout(() => setPersistenceReady(true), 0);
      return () => {
        cancelled = true;
        window.clearTimeout(timer);
      };
    }

    void readBrowserValue<PersistedPageMemory>(pageMemoryKey(userId))
      .then((saved) => {
        if (cancelled || !saved || saved.version !== PAGE_MEMORY_VERSION) return;
        const document = saved.document;
        setSourceType(document.sourceType);
        setContent(document.content);
        setWorkspaceChoice(document.workspaceChoice);
        setNewWorkspaceName(document.newWorkspaceName);
        setPendingWorkspaceId(document.pendingWorkspaceId);
        setWorkspaceConfirmed(document.workspaceConfirmed);
        setWorkspacePendingCreation(document.workspacePendingCreation);
        setSubmitting(document.submitting);
        setActiveTask(document.activeTask);
        setFileUploads(document.fileUploads);
        setUploadKey(document.uploadKey);
        setNeedsUploadRecovery(document.fileUploads.some((upload) =>
          upload.status === "waiting"
          || upload.status === "submitting"
          || upload.status === "queued"
          || upload.status === "running"));

        setQuery(saved.retrieval.query);
        setTopK(saved.retrieval.topK);
        setResult(saved.retrieval.result);
        setElapsed(saved.retrieval.elapsed);

        const restored = sanitizeConversations(saved.chat?.conversations);
        if (restored.length) {
          setConversations(restored);
          setActiveId(restored.some((conversation) => conversation.localId === saved.chat?.activeId)
            ? saved.chat!.activeId
            : restored[0].localId);
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setPersistenceReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [workspace.userId]);

  useEffect(() => {
    if (!persistenceReady || !workspace.userId) return;
    const value: PersistedPageMemory = {
      version: PAGE_MEMORY_VERSION,
      document: {
        sourceType,
        content,
        workspaceChoice,
        newWorkspaceName,
        pendingWorkspaceId,
        workspaceConfirmed,
        workspacePendingCreation,
        submitting,
        activeTask,
        fileUploads,
        uploadKey,
      },
      retrieval: { query, topK, result, elapsed },
      chat: { conversations, activeId },
    };
    const timer = window.setTimeout(() => {
      void writeBrowserValue(pageMemoryKey(workspace.userId), value).catch(() => undefined);
    }, 120);
    return () => window.clearTimeout(timer);
  }, [
    activeTask,
    activeId,
    content,
    conversations,
    elapsed,
    fileUploads,
    newWorkspaceName,
    pendingWorkspaceId,
    persistenceReady,
    query,
    result,
    sourceType,
    submitting,
    topK,
    uploadKey,
    workspace.userId,
    workspaceChoice,
    workspaceConfirmed,
    workspacePendingCreation,
  ]);

  const documentValue = useMemo<DocumentMemoryValue>(() => ({
    persistenceReady, needsUploadRecovery, setNeedsUploadRecovery,
    sourceType, setSourceType, content, setContent, workspaceChoice, setWorkspaceChoice,
    newWorkspaceName, setNewWorkspaceName, pendingWorkspaceId, setPendingWorkspaceId,
    workspaceConfirmed, setWorkspaceConfirmed, workspacePendingCreation, setWorkspacePendingCreation,
    submitting, setSubmitting, activeTask, setActiveTask, fileUploads, setFileUploads,
    uploadKey, setUploadKey, notice, setNotice,
  }), [
    activeTask, content, fileUploads, needsUploadRecovery, newWorkspaceName, notice, pendingWorkspaceId,
    persistenceReady, sourceType, submitting, uploadKey, workspaceChoice, workspaceConfirmed,
    workspacePendingCreation,
  ]);

  const chatValue = useMemo<ChatMemoryValue>(() => ({
    conversations, setConversations, activeId, setActiveId, input, setInput, busy, setBusy,
    status, setStatus, error: chatError, setError: setChatError, abortRef,
  }), [activeId, busy, chatError, conversations, input, status]);

  const retrievalValue = useMemo<RetrievalMemoryValue>(() => ({
    query, setQuery, topK, setTopK, result, setResult, elapsed, setElapsed,
    loading: retrievalLoading, setLoading: setRetrievalLoading,
    error: retrievalError, setError: setRetrievalError,
  }), [elapsed, query, result, retrievalError, retrievalLoading, topK]);

  return (
    <DocumentMemoryContext.Provider value={documentValue}>
      <ChatMemoryContext.Provider value={chatValue}>
        <RetrievalMemoryContext.Provider value={retrievalValue}>
          {children}
        </RetrievalMemoryContext.Provider>
      </ChatMemoryContext.Provider>
    </DocumentMemoryContext.Provider>
  );
}

export function useDocumentMemory(): DocumentMemoryValue {
  const value = useContext(DocumentMemoryContext);
  if (!value) throw new Error("useDocumentMemory 必须在 AppMemoryProvider 中使用");
  return value;
}

export function useChatMemory(): ChatMemoryValue {
  const value = useContext(ChatMemoryContext);
  if (!value) throw new Error("useChatMemory 必须在 AppMemoryProvider 中使用");
  return value;
}

export function useRetrievalMemory(): RetrievalMemoryValue {
  const value = useContext(RetrievalMemoryContext);
  if (!value) throw new Error("useRetrievalMemory 必须在 AppMemoryProvider 中使用");
  return value;
}
