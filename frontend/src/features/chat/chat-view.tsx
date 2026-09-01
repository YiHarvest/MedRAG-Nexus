"use client";

import { Add, Chat, ChatBot, Close, Send, StopFilledAlt, Time, User } from "@carbon/icons-react";
import { InlineNotification, TextArea } from "@carbon/react";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { createConversation, useChatMemory } from "@/components/app-memory";
import type { ChatSource, MemoryConversation, VisibleMessage } from "@/components/app-memory";
import { cancelAgentAction, describeAgentError } from "@/lib/agent-api";
import type {
  AgentActionCardData,
  AgentActionResponse,
  AgentArtifactData,
  AgentInputRequestData,
  AgentInteraction,
  AgentTaskData,
} from "@/lib/agent-types";
import { generateUUID } from "@/lib/uuid";
import { describeWebUiError, openWebUiChatStream } from "@/lib/webui-api";
import { ActionCard } from "./action-card";
import { agentActionTitle } from "./agent-presentation";
import { ArtifactCard } from "./artifact-card";
import { FileInputCard } from "./file-input-card";
import agentStyles from "./agent-card.module.css";
import styles from "./chat-view.module.css";

interface StreamPayload {
  content?: string;
  message?: string;
  conversation_id?: string;
  items?: ChatSource[];
  code?: string;
  [key: string]: unknown;
}

interface AgentInteractionViewProps {
  interaction: AgentInteraction;
  all: AgentInteraction[];
  onChange: (interaction: AgentInteraction) => void;
  onResponse: (response: AgentActionResponse) => void;
  onCancelInput: (request: AgentInputRequestData) => void;
}

function AgentInteractionView({ interaction, all, onChange, onResponse, onCancelInput }: AgentInteractionViewProps) {
  if (interaction.kind === "action") {
    const secureInput = all.find((item): item is AgentInputRequestData => item.kind === "input" && item.input_type === "password" && item.action_id === interaction.action_id);
    return <ActionCard action={interaction} secureInput={secureInput} onChange={onChange} onInputChange={onChange} onResponse={onResponse} />;
  }
  if (interaction.kind === "input") {
    if (interaction.input_type === "file") return <FileInputCard request={interaction} onChange={onChange} onResponse={onResponse} onCancel={() => onCancelInput(interaction)} />;
    if (all.some((item) => item.kind === "action" && item.action_id === interaction.action_id)) return null;
    const action: AgentActionCardData = {
      kind: "action",
      action_id: interaction.action_id,
      title: interaction.title,
      summary: interaction.description,
      status: "pending_input",
      risk_level: "sensitive",
    };
    return <ActionCard action={action} secureInput={interaction} onChange={onChange} onInputChange={onChange} onResponse={onResponse} />;
  }
  if (interaction.kind === "artifact") return <ArtifactCard artifact={interaction} onChange={onChange} />;
  if (interaction.kind === "task") {
    return <section className={agentStyles.card} aria-label={interaction.label || "后台任务"}>
      <div className={agentStyles.header}>
        <div className={agentStyles.title}><strong>{interaction.label || "后台任务"}</strong><span>{interaction.stage || interaction.message || `任务 ${interaction.task_id}`}</span></div>
        <span className={agentStyles.badge} data-status={interaction.status}>{taskStatusLabel(interaction.status)}</span>
      </div>
      {typeof interaction.percent === "number" ? <div className={agentStyles.progress} aria-label={`进度 ${interaction.percent}%`}><span style={{ width: `${Math.max(0, Math.min(100, interaction.percent))}%` }} /></div> : null}
    </section>;
  }
  return <section className={agentStyles.card} role={interaction.status === "error" ? "alert" : "status"}>
    <div className={agentStyles.notice}>
      <div className={agentStyles.title}><strong>{interaction.title || (interaction.status === "error" ? "操作未完成" : "操作结果")}</strong><span>{interaction.message}</span></div>
    </div>
  </section>;
}

export function ChatView() {
  const {
    conversations,
    setConversations,
    activeId,
    setActiveId,
    input,
    setInput,
    busy,
    setBusy,
    status,
    setStatus,
    error,
    setError,
    abortRef,
  } = useChatMemory();
  const [historyOpen, setHistoryOpen] = useState(false);
  const threadRef = useRef<HTMLDivElement | null>(null);
  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.localId === activeId) ?? conversations[0],
    [activeId, conversations],
  );
  const messages = useMemo(() => activeConversation?.messages ?? [], [activeConversation]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, status]);

  function updateConversation(localId: string, update: (item: MemoryConversation) => MemoryConversation) {
    setConversations((current) => current.map((item) => item.localId === localId ? update(item) : item));
  }

  function updateMessageInteraction(localId: string, messageId: string, interaction: AgentInteraction) {
    updateConversation(localId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => {
        if (message.id !== messageId) return message;
        const key = interactionKey(interaction);
        const interactions = [...(message.interactions ?? [])];
        const index = interactions.findIndex((item) => interactionKey(item) === key);
        if (index >= 0) interactions[index] = interaction;
        else interactions.push(interaction);
        return { ...message, interactions };
      }),
    }));
  }

  function updateMessageDelivery(
    localId: string,
    messageId: string,
    delivery: NonNullable<VisibleMessage["delivery"]>,
  ) {
    updateConversation(localId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => (
        message.id === messageId ? { ...message, delivery } : message
      )),
    }));
  }

  function applyActionResponse(localId: string, messageId: string, response: AgentActionResponse) {
    updateConversation(localId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => {
        if (message.id !== messageId) return message;
        const responseInteractions: AgentInteraction[] = [];
        if (response.task) responseInteractions.push({ kind: "task", ...response.task });
        if (response.artifact) responseInteractions.push({ kind: "artifact", ...response.artifact });
        if (response.input) responseInteractions.push({ kind: "input", status: "pending", ...response.input });
        const interactions = (message.interactions ?? []).map((item) => item.kind === "action" && item.action_id === response.action_id ? {
          ...item,
          status: normalizeActionStatus(response.status, item.status),
          result_summary: summaryValue(response.result_summary) ?? item.result_summary,
          error: response.error ?? null,
        } : item);
        return {
          ...message,
          interactions: mergeInteractions(interactions, responseInteractions),
        };
      }),
    }));
  }

  async function cancelInput(localId: string, messageId: string, request: AgentInputRequestData) {
    updateMessageInteraction(localId, messageId, { ...request, status: "submitting", error: null });
    try {
      await cancelAgentAction(request.action_id);
      updateMessageInteraction(localId, messageId, { ...request, status: "cancelled", error: null });
    } catch (reason) {
      updateMessageInteraction(localId, messageId, { ...request, status: "failed", error: describeAgentError(reason) });
    }
  }

  function startConversation() {
    abortRef.current?.abort();
    if (activeConversation?.messages.length === 0) {
      setInput("");
      setStatus("");
      setError(null);
      setBusy(false);
      setHistoryOpen(false);
      return;
    }
    const next = createConversation();
    setConversations((current) => [next, ...current]);
    setActiveId(next.localId);
    setInput("");
    setStatus("");
    setError(null);
    setBusy(false);
  }

  function selectConversation(localId: string) {
    if (localId !== activeId) {
      abortRef.current?.abort();
      setActiveId(localId);
      setInput("");
      setStatus("");
      setError(null);
      setBusy(false);
    }
    setHistoryOpen(false);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = input.trim();
    if (!question || !activeConversation) return;
    if (busy) abortRef.current?.abort();
    const localId = activeConversation.localId;
    const userMessage: VisibleMessage = { id: generateUUID(), role: "user", content: question };
    const assistantId = generateUUID();
    const history = [...activeConversation.messages, userMessage]
      .map((message) => ({ role: message.role, content: messageHistoryContent(message) }))
      .filter((message) => message.content.trim()).slice(-20);

    updateConversation(localId, (conversation) => ({
      ...conversation,
      createdAt: conversation.createdAt ?? Date.now(),
      title: conversation.messages.length === 0 ? compactTitle(question) : conversation.title,
      messages: [
        ...conversation.messages,
        userMessage,
        { id: assistantId, role: "assistant", content: "", delivery: "pending" },
      ],
    }));
    setInput("");
    setBusy(true);
    setStatus("正在连接");
    setError(null);
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      let terminalEventReceived = false;
      const response = await openWebUiChatStream({
        messages: history,
        top_k: 8,
        conversation_id: activeConversation.remoteId,
      }, controller.signal);
      await consumeEventStream(response, (eventName, payload) => {
        if (eventName === "meta" && payload.conversation_id) {
          updateConversation(localId, (conversation) => ({ ...conversation, remoteId: payload.conversation_id! }));
        } else if (eventName === "status") {
          setStatus(payload.message ?? "正在处理");
        } else if (eventName === "tool_start") {
          setStatus("正在查询知识库");
        } else if (eventName === "sources" && payload.items) {
          updateConversation(localId, (conversation) => ({ ...conversation, messages: conversation.messages.map((message) =>
            message.id === assistantId ? { ...message, sources: payload.items } : message) }));
        } else if (eventName === "delta" && payload.content) {
          updateConversation(localId, (conversation) => ({ ...conversation, messages: conversation.messages.map((message) =>
            message.id === assistantId ? { ...message, content: `${message.content}${payload.content}` } : message) }));
        } else if (eventName === "confirmation_required") {
          updateMessageInteraction(localId, assistantId, normalizeAction(payload, "pending_confirmation"));
        } else if (eventName === "input_required") {
          updateMessageInteraction(localId, assistantId, normalizeInput(payload));
        } else if (eventName === "action_result") {
          const interactions = normalizeActionResult(payload);
          interactions.forEach((interaction) => updateMessageInteraction(localId, assistantId, interaction));
        } else if (eventName === "artifact") {
          updateMessageInteraction(localId, assistantId, normalizeArtifact(payload));
        } else if (eventName === "task") {
          updateMessageInteraction(localId, assistantId, normalizeTask(payload));
        } else if (eventName === "permission_denied") {
          updateMessageInteraction(localId, assistantId, {
            kind: "result",
            title: "权限不足",
            message: payload.message ?? "当前账号没有执行此操作的权限。",
            status: "error",
            code: payload.code,
          });
        } else if (eventName === "error") {
          terminalEventReceived = true;
          updateMessageDelivery(localId, assistantId, "failed");
          setError(payload.message ?? "聊天失败，请稍后重试。");
        } else if (eventName === "done") {
          terminalEventReceived = true;
          updateMessageDelivery(localId, assistantId, "complete");
          setStatus("");
        }
      });
      if (!terminalEventReceived) {
        throw new Error("聊天数据流意外中断，请重试。");
      }
    } catch (reason) {
      const stopped = controller.signal.aborted
        || (reason instanceof DOMException && reason.name === "AbortError");
      updateMessageDelivery(localId, assistantId, stopped ? "stopped" : "failed");
      if (!stopped) setError(describeWebUiError(reason));
    } finally {
      if (abortRef.current === controller) {
        setBusy(false);
        setStatus("");
        abortRef.current = null;
      }
    }
  }

  return (
    <div className={`${styles.page} page`} data-history-open={historyOpen}>
      <section className={styles.workspace} aria-label="知识库聊天">
        <header className={styles.toolbar}>
          <div className={styles.toolbarActions}>
            <button className={styles.toolbarButton} type="button" onClick={startConversation}><Add size={18} /><span>新对话</span></button>
            <button className={styles.toolbarButton} data-active={historyOpen} type="button" aria-controls="chat-history" aria-expanded={historyOpen} onClick={() => setHistoryOpen((current) => !current)}>
              <Time size={18} /><span>历史</span>
            </button>
          </div>
        </header>

        <div className={styles.thread} aria-live="polite" ref={threadRef}>
          {messages.length === 0 ? (
            <div className={styles.welcome}>
              <span><Chat size={28} /></span>
              <h1>有什么可以帮你？</h1>
              <p>询问知识库中的内容，也可以处理日常问题。</p>
              <div className={styles.suggestions} aria-label="示例问题">
                {["总结我的知识库内容", "有哪些文档？", "帮我梳理一个方案"].map((suggestion) => (
                  <button key={suggestion} type="button" onClick={() => setInput(suggestion)}>{suggestion}</button>
                ))}
              </div>
            </div>
          ) : (
            <div className={styles.messages}>
              {messages.map((message) => (
                <article className={styles.message} data-role={message.role} key={message.id}>
                  <span className={styles.avatar} aria-hidden="true">{message.role === "user" ? <User size={17} /> : <ChatBot size={17} />}</span>
                  <div className={styles.messageMain}>
                    <div className={styles.messageLabel}>{message.role === "user" ? "你" : "知识助手"}</div>
                    {message.content || !(message.interactions?.length) ? <div
                      className={styles.messageContent}
                      data-long={message.role === "assistant" && message.content.length > 260 ? "true" : undefined}
                    >
                      {message.role === "assistant" && message.content ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
                          a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
                          img: () => null,
                          table: ({ children }) => <div className={styles.markdownTable}><table>{children}</table></div>,
                        }}>{message.content}</ReactMarkdown>
                      ) : message.content || (
                        message.role === "assistant" && message.delivery === "pending"
                          ? "正在思考…"
                          : message.role === "assistant" && message.delivery === "failed"
                            ? "生成中断，请重试。"
                            : "已停止生成"
                      )}
                    </div> : null}
                    {message.interactions?.length ? <div className={styles.interactions}>{message.interactions.map((interaction) => (
                      <AgentInteractionView
                        key={interactionKey(interaction)}
                        interaction={interaction}
                        all={message.interactions ?? []}
                        onChange={(next) => updateMessageInteraction(activeConversation.localId, message.id, next)}
                        onResponse={(response) => applyActionResponse(activeConversation.localId, message.id, response)}
                        onCancelInput={(request) => void cancelInput(activeConversation.localId, message.id, request)}
                      />
                    ))}</div> : null}
                    {message.sources?.length ? (
                      <details className={styles.sources}>
                        <summary>{message.sources.length} 个引用来源</summary>
                        <div className={styles.sourceList}>{message.sources.map((source) => (
                          <article key={`${source.workspace_id}-${source.chunk_id}`}>
                            <strong>[{source.citation_id}] {source.file_name || "文本内容"}</strong>
                            <span>{source.workspace_name}{source.page_number ? `，第 ${source.page_number} 页` : ""}</span>
                            <p>{source.excerpt}</p>
                          </article>
                        ))}</div>
                      </details>
                    ) : null}
                  </div>
                </article>
              ))}
            </div>
          )}
        </div>

        <div className={styles.composerWrap}>
          {error ? <InlineNotification kind="error" title="聊天失败" subtitle={error} lowContrast onCloseButtonClick={() => setError(null)} /> : null}
          {status ? <p className={styles.status} role="status">{status}</p> : null}
          <form className={styles.composer} onSubmit={handleSubmit}>
            <TextArea id="chat-input" labelText="消息" hideLabel placeholder="询问知识库，或输入任何想聊的内容" rows={2} value={input}
              onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }} />
            {busy ? (
              <button className={`${styles.sendButton} ${styles.stopButton}`} type="button" onClick={() => abortRef.current?.abort()}><StopFilledAlt size={18} /><span>停止</span></button>
            ) : (
              <button className={styles.sendButton} type="submit" disabled={!input.trim()}><Send size={18} /><span>发送</span></button>
            )}
          </form>
          <p className={styles.disclaimer}>回答可能有误，请结合引用来源核对。</p>
        </div>
      </section>

      {historyOpen ? <button className={styles.historyBackdrop} type="button" aria-label="关闭历史对话" onClick={() => setHistoryOpen(false)} /> : null}
      <aside className={styles.history} id="chat-history" data-open={historyOpen} aria-hidden={!historyOpen} inert={!historyOpen}>
        <header className={styles.historyHeader}>
          <div><strong>历史对话</strong></div>
          <button type="button" aria-label="收起历史对话" onClick={() => setHistoryOpen(false)}><Close size={18} /></button>
        </header>
        <div className={styles.historyList}>{conversations.map((conversation) => (
          <button key={conversation.localId} type="button" data-active={conversation.localId === activeId} onClick={() => selectConversation(conversation.localId)}>
            <span className={styles.historyItemCopy}>
              <strong>{conversation.title}</strong>
              <small>{conversationSummary(conversation)}</small>
            </span>
            {conversation.createdAt ? <time dateTime={new Date(conversation.createdAt).toISOString()}>{formatTime(conversation.createdAt)}</time> : null}
          </button>
        ))}</div>
      </aside>
    </div>
  );
}

function compactTitle(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > 24 ? `${normalized.slice(0, 24)}…` : normalized;
}

function conversationSummary(conversation: MemoryConversation): string {
  const latest = [...conversation.messages].reverse().find((message) => messageHistoryContent(message).trim());
  if (!latest) return "还没有消息";
  const normalized = messageHistoryContent(latest).replace(/\s+/g, " ").trim();
  return normalized.length > 36 ? `${normalized.slice(0, 36)}…` : normalized;
}

function messageHistoryContent(message: VisibleMessage): string {
  const interactionSummaries = (message.interactions ?? []).flatMap((interaction) => {
    if (interaction.kind === "action") return [`[操作：${interaction.title}；状态：${interaction.status}${interaction.result_summary ? `；结果：${interaction.result_summary}` : ""}]`];
    if (interaction.kind === "artifact") return [`[已生成文件：${interaction.file_name}]`];
    if (interaction.kind === "task") return [`[任务：${interaction.label || interaction.task_id}；状态：${interaction.status}]`];
    if (interaction.kind === "result") return [`[操作结果：${interaction.message}]`];
    if (interaction.status === "submitted") return ["[安全输入已直接提交给服务端，具体内容不可见]"];
    return [];
  });
  return [message.content, ...interactionSummaries].filter(Boolean).join("\n\n");
}

function formatTime(value: number): string {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(value);
}

function interactionKey(interaction: AgentInteraction): string {
  if (interaction.kind === "action" || interaction.kind === "input") return `${interaction.kind}:${interaction.action_id}`;
  if (interaction.kind === "artifact") return `artifact:${interaction.artifact_id}`;
  if (interaction.kind === "task") return `task:${interaction.task_id}`;
  return `result:${interaction.code ?? interaction.title ?? interaction.message}`;
}

function mergeInteractions(current: AgentInteraction[], incoming: AgentInteraction[]): AgentInteraction[] {
  const merged = [...current];
  for (const interaction of incoming) {
    const index = merged.findIndex((item) => interactionKey(item) === interactionKey(interaction));
    if (index >= 0) merged[index] = interaction;
    else merged.push(interaction);
  }
  return merged;
}

function normalizeAction(payload: StreamPayload, fallbackStatus: AgentActionCardData["status"]): AgentActionCardData {
  const source = nestedRecord(payload, "action") ?? payload;
  const confirmationSource = nestedRecord(source, "confirmation") ?? nestedRecord(source, "required_input");
  const target = nestedRecord(source, "target");
  const toolName = stringValue(source.tool_name);
  const argumentsValue = nestedRecord(source, "canonical_arguments");
  const presentation = actionPresentation(toolName, argumentsValue ?? undefined, target ?? undefined);
  const confirmationType = stringValue(confirmationSource?.mode) ?? stringValue(confirmationSource?.type) ?? stringValue(source.confirmation_mode);
  return {
    kind: "action",
    action_id: stringValue(source.action_id) ?? `action-${generateUUID()}`,
    tool_name: toolName,
    title: stringValue(source.title) ?? presentation.title,
    summary: stringValue(source.summary) ?? stringValue(source.message) ?? presentation.summary,
    details: presentation.details,
    risk_level: normalizeRiskLevel(source.risk_level),
    status: normalizeActionStatus(source.status, fallbackStatus),
    confirmation: confirmationSource || confirmationType ? {
      mode: confirmationType === "confirm_name" || confirmationType === "type_name" || confirmationType === "typed_text" ? "type_name" : "click",
      expected_label: stringValue(confirmationSource?.expected_label) ?? stringValue(confirmationSource?.confirm_name) ?? (target ? stringValue(target.display_name) : undefined),
      prompt: stringValue(confirmationSource?.prompt),
    } : { mode: "click" },
    result_summary: summaryValue(source.result_summary),
    error: nullableString(source.error),
    expires_at: nullableString(source.expires_at),
  };
}

function normalizeInput(payload: StreamPayload): AgentInputRequestData {
  const source = nestedRecord(payload, "input") ?? nestedRecord(payload, "required_input") ?? payload;
  const inputType = stringValue(source.input_type) ?? stringValue(source.type);
  const rawFields = Array.isArray(source.fields) ? source.fields : [];
  return {
    kind: "input",
    action_id: stringValue(source.action_id) ?? stringValue(payload.action_id) ?? `action-${generateUUID()}`,
    input_type: inputType === "file" || inputType === "files" || inputType === "upload" ? "file" : "password",
    title: stringValue(source.title) ?? (inputType === "file" ? "请选择文件" : "请输入安全信息"),
    description: stringValue(source.description) ?? stringValue(source.message),
    status: "pending",
    fields: rawFields.flatMap((item) => {
      if (!isRecord(item) || !stringValue(item.name)) return [];
      return [{
        name: stringValue(item.name)!,
        label: stringValue(item.label) ?? stringValue(item.name)!,
        min_length: numberValue(item.min_length),
        autocomplete: stringValue(item.autocomplete),
      }];
    }),
    accept: stringValue(source.accept),
    multiple: booleanValue(source.multiple),
    max_files: numberValue(source.max_files),
  };
}

function normalizeArtifact(payload: StreamPayload): AgentArtifactData {
  const source = nestedRecord(payload, "artifact") ?? payload;
  return {
    kind: "artifact",
    artifact_id: stringValue(source.artifact_id) ?? `artifact-${generateUUID()}`,
    file_name: stringValue(source.file_name) ?? "导出文件.docx",
    mime_type: stringValue(source.mime_type),
    size_bytes: numberValue(source.size_bytes),
    expires_at: nullableString(source.expires_at),
    revoked_at: nullableString(source.revoked_at),
    status: normalizeArtifactStatus(source.status, nullableString(source.revoked_at), nullableString(source.expires_at)),
  };
}

function normalizeTask(payload: StreamPayload | Record<string, unknown>): AgentTaskData {
  const source = nestedRecord(payload, "task") ?? payload;
  return {
    kind: "task",
    task_id: stringValue(source.task_id) ?? `task-${generateUUID()}`,
    label: stringValue(source.label) ?? stringValue(source.title),
    status: normalizeTaskStatus(source.status),
    stage: stringValue(source.stage),
    percent: numberValue(source.percent),
    message: stringValue(source.message),
  };
}

function normalizeActionResult(payload: StreamPayload): AgentInteraction[] {
  const output: AgentInteraction[] = [];
  const actionSource = nestedRecord(payload, "action") ?? (stringValue(payload.action_id) ? payload : null);
  if (actionSource) output.push(normalizeAction(actionSource, "succeeded"));
  const artifact = nestedRecord(payload, "artifact");
  if (artifact) output.push(normalizeArtifact(artifact));
  const task = nestedRecord(payload, "task");
  if (task) output.push(normalizeTask(task));
  if (!output.length || (payload.message && !actionSource)) {
    output.push({
      kind: "result",
      title: stringValue(payload.title),
      message: payload.message ?? "操作已完成。",
      status: payload.code === "permission_denied" || payload.status === "failed" ? "error" : "success",
      code: payload.code,
    });
  }
  return output;
}

function nestedRecord(value: Record<string, unknown>, key: string): Record<string, unknown> | null {
  return isRecord(value[key]) ? value[key] : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length ? value : undefined;
}

function nullableString(value: unknown): string | null | undefined {
  return value === null ? null : stringValue(value);
}

function summaryValue(value: unknown): string | null | undefined {
  if (value === null || value === undefined || typeof value === "string") return value;
  if (isRecord(value) && typeof value.message === "string") return value.message;
  return "操作已完成。";
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function booleanValue(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function normalizeRiskLevel(value: unknown): AgentActionCardData["risk_level"] {
  return value === "read" || value === "write" || value === "sensitive" || value === "destructive" ? value : undefined;
}

function normalizeActionStatus(value: unknown, fallback: AgentActionCardData["status"]): AgentActionCardData["status"] {
  if (value === "completed") return "succeeded";
  if (value === "executing") return "running";
  if (value === "pending") return fallback;
  return ["pending_confirmation", "pending_input", "confirmed", "queued", "running", "succeeded", "failed", "cancelled", "expired"].includes(String(value))
    ? value as AgentActionCardData["status"]
    : fallback;
}

function normalizeArtifactStatus(value: unknown, revokedAt?: string | null, expiresAt?: string | null): AgentArtifactData["status"] {
  if (revokedAt) return "revoked";
  if (expiresAt && new Date(expiresAt).getTime() <= Date.now()) return "expired";
  return value === "revoked" || value === "expired" || value === "available" ? value : "available";
}

const ACTION_FIELD_LABELS: Record<string, string> = {
  user_id: "知识域 ID",
  user_name: "知识域名称",
  workspace_id: "知识库 ID",
  workspace_name: "知识库名称",
  account_id: "账号 ID",
  login_name: "登录名",
  display_name: "显示名称",
  permission_level: "权限等级",
  group_key: "权限组标识",
  group_keys: "所属权限组",
  read_min_level: "最低读取等级",
  workspace_create_min_level: "新建知识库最低等级",
  cud_min_level: "最低编辑等级",
  file_id: "文件 ID",
  file_name: "文件名",
  content_hash: "文本资料标识",
  artifact_id: "下载文件 ID",
  content: "文本内容",
  action: "授权能力",
  bindings: "授权配置",
  enabled: "账号状态",
  bound: "绑定操作",
  must_change_password: "登录后必须修改密码",
};

function actionPresentation(
  toolName: string | undefined,
  argumentsValue: Record<string, unknown> | undefined,
  target: Record<string, unknown> | undefined,
): Pick<AgentActionCardData, "title" | "summary" | "details"> {
  const details = Object.entries(argumentsValue ?? {}).flatMap(([key, value]) => {
    if (key === "confirm_name" || value === null || value === undefined) return [];
    const label = ACTION_FIELD_LABELS[key];
    if (!label) return [];
    return [{ label, value: actionFieldValue(key, value) }];
  });
  if (!details.length && target) {
    const targetName = stringValue(target.display_name);
    if (targetName) details.push({ label: "操作对象", value: targetName });
  }
  return {
    title: toolName === "bind_account_to_user"
      ? argumentsValue?.bound === false ? "移除账号知识域绑定" : "增加账号知识域绑定"
      : agentActionTitle(toolName),
    summary: "请核对以下信息，确认后将立即执行。",
    details,
  };
}

function actionFieldValue(key: string, value: unknown): string {
  if (typeof value === "boolean") {
    if (key === "enabled") return value ? "启用" : "停用";
    if (key === "bound") return value ? "增加绑定" : "移除绑定";
    return value ? "是" : "否";
  }
  if (typeof value === "string") return value.length > 160 ? `${value.slice(0, 160)}…` : value;
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) {
    if (value.every((item) => typeof item === "string")) return value.length ? value.join("、") : "无";
    return `${value.length} 项配置`;
  }
  if (isRecord(value)) return `${Object.keys(value).length} 项配置`;
  return String(value);
}

function normalizeTaskStatus(value: unknown): AgentTaskData["status"] {
  if (value === "completed") return "succeeded";
  return value === "queued" || value === "running" || value === "succeeded" || value === "failed" || value === "cancelled" ? value : "queued";
}

function taskStatusLabel(status: AgentTaskData["status"]): string {
  return ({ queued: "已排队", running: "执行中", succeeded: "已完成", failed: "失败", cancelled: "已取消" })[status];
}

async function consumeEventStream(response: Response, onEvent: (eventName: string, payload: StreamPayload) => void): Promise<void> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consumeFrame = (frame: string) => {
    let eventName = "message";
    const data: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return;
    try { onEvent(eventName, JSON.parse(data.join("\n")) as StreamPayload); }
    catch { onEvent("error", { message: "聊天服务返回了无法解析的数据。" }); }
  };
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    frames.forEach(consumeFrame);
    if (done) break;
  }
  if (buffer.trim()) consumeFrame(buffer);
}
