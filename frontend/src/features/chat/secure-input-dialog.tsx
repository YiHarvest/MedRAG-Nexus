"use client";

import { type FormEvent, useMemo, useState } from "react";
import type { AgentInputRequestData } from "@/lib/agent-types";
import styles from "./agent-card.module.css";

interface SecureInputDialogProps {
  request: AgentInputRequestData;
  busy: boolean;
  error?: string | null;
  onCancel: () => void;
  onSubmit: (values: Record<string, string>) => void;
}

export function SecureInputDialog({ request, busy, error, onCancel, onSubmit }: SecureInputDialogProps) {
  const fields = useMemo(() => request.fields?.length ? request.fields : [
    { name: "new_password", label: "新密码", min_length: 3, autocomplete: "new-password" },
  ], [request.fields]);
  const [values, setValues] = useState<Record<string, string>>({});
  const valid = fields.every((field) => (values[field.name] ?? "").length >= (field.min_length ?? 1));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (valid) onSubmit(values);
  }

  return (
    <div className={styles.dialogBackdrop} role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onCancel();
    }}>
      <form className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby={`secure-${request.action_id}`} onSubmit={submit}>
        <h2 id={`secure-${request.action_id}`}>{request.title}</h2>
        <p>{request.description || "密码将直接提交给服务端，不会发送给模型或写入聊天记录。"}</p>
        {fields.map((field) => (
          <div className={styles.field} key={field.name}>
            <label htmlFor={`secure-${request.action_id}-${field.name}`}>{field.label}</label>
            <input id={`secure-${request.action_id}-${field.name}`} type="password" required minLength={field.min_length ?? 1} autoComplete={field.autocomplete ?? "off"} value={values[field.name] ?? ""} disabled={busy} onChange={(event) => setValues((current) => ({ ...current, [field.name]: event.target.value }))} />
          </div>
        ))}
        {error ? <p className={styles.error} role="alert">{error}</p> : null}
        <div className={styles.actions}>
          <button className={`${styles.button} ${styles.buttonPrimary}`} type="submit" disabled={busy || !valid}>{busy ? "正在提交…" : "安全提交"}</button>
          <button className={styles.button} type="button" disabled={busy} onClick={onCancel}>取消</button>
        </div>
      </form>
    </div>
  );
}

