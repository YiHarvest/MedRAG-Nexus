"use client";

import { useState } from "react";
import { artifactDownloadUrl, describeAgentError, revokeAgentArtifact } from "@/lib/agent-api";
import type { AgentArtifactData } from "@/lib/agent-types";
import styles from "./agent-card.module.css";

export function ArtifactCard({ artifact, onChange }: { artifact: AgentArtifactData; onChange: (artifact: AgentArtifactData) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const unavailable = Boolean(artifact.revoked_at) || artifact.status === "revoked" || artifact.status === "expired";

  async function revoke() {
    setBusy(true);
    setError(null);
    try {
      await revokeAgentArtifact(artifact.artifact_id);
      onChange({ ...artifact, status: "revoked", revoked_at: new Date().toISOString() });
    } catch (reason) {
      setError(describeAgentError(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={styles.card} aria-label={`导出文件 ${artifact.file_name}`}>
      <div className={styles.header}>
        <div className={styles.title}><strong>{artifact.file_name}</strong><span>{artifact.size_bytes === undefined ? "临时导出文件" : formatBytes(artifact.size_bytes)}{artifact.expires_at ? ` · ${formatExpiry(artifact.expires_at)}` : ""}</span></div>
        <span className={styles.badge} data-status={unavailable ? artifact.status ?? "revoked" : "available"}>{unavailable ? artifact.status === "expired" ? "已过期" : "已撤销" : "可下载"}</span>
      </div>
      {error ? <p className={styles.error} role="alert">{error}</p> : null}
      <div className={styles.actions}>
        {!unavailable ? <a className={`${styles.button} ${styles.buttonPrimary}`} href={artifactDownloadUrl(artifact.artifact_id)}>下载</a> : null}
        {!unavailable ? <button className={`${styles.button} ${styles.buttonDanger}`} type="button" disabled={busy} onClick={() => void revoke()}>{busy ? "正在撤销…" : "撤销链接"}</button> : null}
      </div>
    </section>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatExpiry(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "24 小时内有效" : `${date.toLocaleString("zh-CN")} 过期`;
}

