"use client";

import { type ChangeEvent, useRef, useState } from "react";
import { uploadAgentFiles, describeAgentError } from "@/lib/agent-api";
import type { AgentActionResponse, AgentInputRequestData } from "@/lib/agent-types";
import styles from "./agent-card.module.css";

interface FileInputCardProps {
  request: AgentInputRequestData;
  onChange: (request: AgentInputRequestData) => void;
  onResponse: (response: AgentActionResponse) => void;
  onCancel: () => void;
}

export function FileInputCard({ request, onChange, onResponse, onCancel }: FileInputCardProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const maxFiles = request.max_files ?? (request.multiple ? 20 : 1);

  function selectFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []).slice(0, maxFiles);
    setFiles(selected);
    setError(null);
  }

  async function submit() {
    if (!files.length) return;
    setBusy(true);
    setError(null);
    onChange({ ...request, status: "submitting", error: null });
    try {
      const response = await uploadAgentFiles(request.action_id, files);
      setFiles([]);
      onChange({ ...request, status: "submitted", error: null });
      onResponse(response);
    } catch (reason) {
      const message = describeAgentError(reason);
      setError(message);
      onChange({ ...request, status: "failed", error: message });
    } finally {
      setBusy(false);
    }
  }

  const terminal = request.status === "submitted" || request.status === "cancelled";
  return (
    <section className={styles.card} aria-label={request.title}>
      <div className={styles.header}>
        <div className={styles.title}><strong>{request.title}</strong><span>{request.description || "从本机选择要上传的文件。"}</span></div>
        <span className={styles.badge} data-status={request.status}>{request.status === "submitted" ? "已提交" : request.status === "cancelled" ? "已取消" : "需要文件"}</span>
      </div>
      {!terminal ? <>
        <input className={styles.fileInput} ref={inputRef} type="file" accept={request.accept} multiple={request.multiple} onChange={selectFiles} />
        {files.length ? <ul className={styles.fileList}>{files.map((file) => <li key={`${file.name}-${file.lastModified}`}><span>{file.name}</span><span>{formatBytes(file.size)}</span></li>)}</ul> : null}
        {error || request.error ? <p className={styles.error} role="alert">{error || request.error}</p> : null}
        <div className={styles.actions}>
          <button className={styles.button} type="button" disabled={busy} onClick={() => inputRef.current?.click()}>{files.length ? "重新选择" : "选择文件"}</button>
          <button className={`${styles.button} ${styles.buttonPrimary}`} type="button" disabled={busy || !files.length} onClick={() => void submit()}>{busy ? "正在上传…" : "上传"}</button>
          <button className={styles.button} type="button" disabled={busy} onClick={onCancel}>取消</button>
        </div>
      </> : null}
    </section>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

