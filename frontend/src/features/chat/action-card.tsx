"use client";

import { useState } from "react";
import { cancelAgentAction, confirmAgentAction, describeAgentError, submitAgentSecureInput } from "@/lib/agent-api";
import type { AgentActionCardData, AgentActionResponse, AgentInputRequestData } from "@/lib/agent-types";
import { agentActionTitle } from "./agent-presentation";
import { ConfirmationDialog } from "./confirmation-dialog";
import { SecureInputDialog } from "./secure-input-dialog";
import styles from "./agent-card.module.css";

interface ActionCardProps {
  action: AgentActionCardData;
  secureInput?: AgentInputRequestData;
  onChange: (action: AgentActionCardData) => void;
  onInputChange?: (request: AgentInputRequestData) => void;
  onResponse: (response: AgentActionResponse) => void;
}

export function ActionCard({ action, secureInput, onChange, onInputChange, onResponse }: ActionCardProps) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [secureOpen, setSecureOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = action.status === "pending_confirmation";
  const terminal = ["succeeded", "failed", "cancelled", "expired"].includes(action.status);
  const displayTitle = agentActionTitle(action.tool_name, action.title);

  async function confirm(confirmationText?: string) {
    setBusy(true);
    setError(null);
    try {
      const response = await confirmAgentAction(action.action_id, confirmationText);
      onChange({ ...action, status: responseStatus(response.status, action.status), result_summary: summarizeResult(response.result_summary), error: response.error });
      onResponse(response);
      setConfirmOpen(false);
    } catch (reason) {
      setError(describeAgentError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function submitSecure(values: Record<string, string>) {
    if (!secureInput) return;
    setBusy(true);
    setError(null);
    try {
      const response = await submitAgentSecureInput(action.action_id, values);
      onInputChange?.({ ...secureInput, status: "submitted", error: null });
      onChange({ ...action, status: responseStatus(response.status, action.status), result_summary: summarizeResult(response.result_summary), error: response.error });
      onResponse(response);
      setSecureOpen(false);
    } catch (reason) {
      setError(describeAgentError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setBusy(true);
    setError(null);
    try {
      const response = await cancelAgentAction(action.action_id);
      onChange({ ...action, status: response ? responseStatus(response.status, "cancelled") : "cancelled", result_summary: summarizeResult(response?.result_summary), error: response?.error });
      if (response) onResponse(response);
    } catch (reason) {
      setError(describeAgentError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <section className={styles.card} aria-label={displayTitle}>
        <div className={styles.header}>
          <div className={styles.title}><strong>{displayTitle}</strong><span>{action.summary || "请核对操作信息后确认。"}</span></div>
          <span className={styles.badge} data-status={action.status}>{statusLabel(action.status)}</span>
        </div>
        {action.details?.length ? <dl className={styles.details}>{action.details.map((item) => <div key={`${item.label}:${item.value}`}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl> : null}
        {action.result_summary ? <p className={styles.copy}>{action.result_summary}</p> : null}
        {error || action.error ? <p className={styles.error} role="alert">{error || action.error}</p> : null}
        {!terminal ? <div className={styles.actions}>
          {pending ? <button className={`${styles.button} ${styles.buttonDanger}`} type="button" disabled={busy} onClick={() => setConfirmOpen(true)}>查看并确认</button> : null}
          {secureInput?.status === "pending" && !pending ? <button className={`${styles.button} ${styles.buttonPrimary}`} type="button" disabled={busy} onClick={() => setSecureOpen(true)}>填写安全信息</button> : null}
          {(pending || secureInput?.status === "pending") ? <button className={styles.button} type="button" disabled={busy} onClick={() => void cancel()}>{busy ? "处理中…" : "取消操作"}</button> : null}
        </div> : null}
      </section>
      {confirmOpen ? <ConfirmationDialog action={{ ...action, title: displayTitle }} busy={busy} error={error} onCancel={() => setConfirmOpen(false)} onConfirm={(value) => void confirm(value)} /> : null}
      {secureOpen && secureInput ? <SecureInputDialog request={secureInput} busy={busy} error={error} onCancel={() => setSecureOpen(false)} onSubmit={(values) => void submitSecure(values)} /> : null}
    </>
  );
}

function statusLabel(status: AgentActionCardData["status"]): string {
  return ({
    pending_confirmation: "等待确认", pending_input: "等待输入", confirmed: "已确认", queued: "已排队",
    running: "执行中", succeeded: "已完成", failed: "失败", cancelled: "已取消", expired: "已过期",
  })[status];
}

function responseStatus(status: AgentActionResponse["status"], fallback: AgentActionCardData["status"]): AgentActionCardData["status"] {
  if (status === "pending") return fallback;
  if (status === "executing") return "running";
  return status;
}

function summarizeResult(value: unknown): string | null | undefined {
  if (value === null || value === undefined || typeof value === "string") return value;
  if (typeof value === "object" && value && "message" in value && typeof value.message === "string") return value.message;
  return "操作已完成。";
}
