import { DocumentBlank } from "@carbon/icons-react";
import { InlineLoading, Tag } from "@carbon/react";
import type { ReactNode } from "react";

export function EmptyState({ title, description, icon }: {
  title: string;
  description: string;
  icon?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div>
        <span className="empty-state-icon">
          {icon ?? <DocumentBlank size={28} />}
        </span>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
    </div>
  );
}

export function LoadingState({ label = "正在加载" }: { label?: string }) {
  return (
    <div className="empty-state" role="status">
      <InlineLoading description={label} />
    </div>
  );
}

export function StatusTag({ status }: { status: string }) {
  const type = status === "ok" || status === "succeeded" || status === "delivered"
    ? "green"
    : status === "degraded" || status === "running" || status === "delivering"
      ? "purple"
      : status === "unavailable" || status === "failed"
        ? "red"
        : "blue";

  return <Tag type={type}>{status}</Tag>;
}
