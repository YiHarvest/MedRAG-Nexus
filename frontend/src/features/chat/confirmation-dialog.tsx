"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";
import type { AgentActionCardData } from "@/lib/agent-types";
import styles from "./agent-card.module.css";

interface ConfirmationDialogProps {
  action: AgentActionCardData;
  busy: boolean;
  error?: string | null;
  onCancel: () => void;
  onConfirm: (confirmationText?: string) => void;
}

export function ConfirmationDialog({ action, busy, error, onCancel, onConfirm }: ConfirmationDialogProps) {
  const [confirmationText, setConfirmationText] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const expected = action.confirmation?.expected_label ?? "";
  const requiresName = action.confirmation?.mode === "type_name";
  const canSubmit = !requiresName || (Boolean(expected) && confirmationText === expected);

  useEffect(() => {
    if (requiresName) inputRef.current?.focus();
  }, [requiresName]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    onConfirm(requiresName ? confirmationText : undefined);
  }

  return (
    <div className={styles.dialogBackdrop} role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onCancel();
    }}>
      <form className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby={`confirm-${action.action_id}`} onSubmit={submit}>
        <h2 id={`confirm-${action.action_id}`}>{action.title}</h2>
        <p>{action.summary || action.confirmation?.prompt || "请确认是否执行这项高风险操作。"}</p>
        {action.details?.length ? <dl className={styles.details}>{action.details.map((item) => <div key={`${item.label}:${item.value}`}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}</dl> : null}
        {requiresName ? (
          <div className={styles.field}>
            <label htmlFor={`confirm-name-${action.action_id}`}>{expected ? `输入“${expected}”以确认` : "服务端未提供目标名称，暂时无法确认"}</label>
            <input id={`confirm-name-${action.action_id}`} ref={inputRef} autoComplete="off" value={confirmationText} disabled={busy} onChange={(event) => setConfirmationText(event.target.value)} />
          </div>
        ) : null}
        {error ? <p className={styles.error} role="alert">{error}</p> : null}
        <div className={styles.actions}>
          <button className={`${styles.button} ${styles.buttonDanger}`} type="submit" disabled={busy || !canSubmit}>{busy ? "正在确认…" : "确认执行"}</button>
          <button className={styles.button} type="button" disabled={busy} onClick={onCancel}>返回</button>
        </div>
      </form>
    </div>
  );
}
