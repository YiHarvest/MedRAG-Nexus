"use client";

import { Activity, Renew } from "@carbon/icons-react";
import { Button, InlineNotification } from "@carbon/react";
import { useCallback, useEffect, useState } from "react";
import { useAccount } from "@/components/account";
import { EmptyState, LoadingState, StatusTag } from "@/components/states";
import { describeWebUiError, getWebUiHealth } from "@/lib/webui-api";
import type { HealthResponse } from "@/lib/types";
import styles from "./system-view.module.css";

interface HealthSnapshot {
  live: HealthResponse;
  ready: HealthResponse;
  checkedAt: Date;
  elapsedMs: number;
}

export function SystemView() {
  const { can } = useAccount();
  const canReadHealth = can("webui.system.read");
  const [snapshot, setSnapshot] = useState<HealthSnapshot | null>(null);
  const [healthLoading, setHealthLoading] = useState(canReadHealth);
  const [healthError, setHealthError] = useState<string | null>(null);

  const refreshHealth = useCallback(async () => {
    if (!canReadHealth) return;
    setHealthLoading(true);
    setHealthError(null);
    const startedAt = performance.now();
    try {
      const ready = await getWebUiHealth();
      setSnapshot({
        live: ready,
        ready,
        checkedAt: new Date(),
        elapsedMs: performance.now() - startedAt,
      });
    } catch (caught) {
      setHealthError(describeWebUiError(caught));
    } finally {
      setHealthLoading(false);
    }
  }, [canReadHealth]);

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshHealth(), 0);
    return () => window.clearTimeout(timer);
  }, [refreshHealth]);

  const dependencies = Object.entries(snapshot?.ready.dependencies ?? {});
  const healthyCount = dependencies.filter(([, state]) => state.status === "ok").length;

  return (
    <div className="page">
      {canReadHealth ? (
        <section className={styles.section} aria-labelledby="system-health-title">
          <div className={styles.sectionHeader}>
            <div><p className="page-eyebrow">System health</p><h1 id="system-health-title" className="panel-heading">服务健康</h1></div>
            <Button kind="tertiary" renderIcon={Renew} disabled={healthLoading} onClick={() => void refreshHealth()}>
              {healthLoading ? "检查中" : "重新检查"}
            </Button>
          </div>

          {healthError ? (
            <InlineNotification kind="error" title="健康检查请求失败" subtitle={healthError} onCloseButtonClick={() => setHealthError(null)} />
          ) : null}

          {healthLoading && !snapshot ? (
            <LoadingState label="正在检查服务与依赖" />
          ) : snapshot ? (
            <>
              <div className="metric-grid" aria-label="总体健康指标">
                <div className="metric"><div className="metric-label">FastAPI 进程</div><div className="metric-value"><StatusTag status={snapshot.live.status} /></div></div>
                <div className="metric"><div className="metric-label">服务就绪</div><div className="metric-value"><StatusTag status={snapshot.ready.status} /></div></div>
                <div className="metric"><div className="metric-label">健康依赖</div><div className="metric-value">{healthyCount} / {dependencies.length}</div></div>
              </div>

              {snapshot.ready.status !== "ok" ? (
                <InlineNotification className="inline-notice-bottom" kind={snapshot.ready.status === "unavailable" ? "error" : "warning"} lowContrast hideCloseButton title={snapshot.ready.status === "unavailable" ? "核心依赖不可用" : "服务处于降级状态"} subtitle="查看下方依赖详情。MinerU 或 Rerank 等可降级依赖异常时，部分链路仍可能继续工作。" />
              ) : null}

              {dependencies.length > 0 ? (
                <div className="health-grid" aria-label="依赖健康详情">
                  {dependencies.map(([name, state]) => (
                    <article className="health-card" key={name}>
                      <div className="health-card-head"><div><p className="page-eyebrow">Dependency</p><h2 className="panel-heading">{formatDependencyName(name)}</h2></div><StatusTag status={state.status} /></div>
                      <div className="health-latency">{state.latency_ms === null || state.latency_ms === undefined ? "暂无耗时" : `${Math.round(state.latency_ms)} ms`}</div>
                      {state.error ? <p className="health-error">{state.error}</p> : null}
                    </article>
                  ))}
                </div>
              ) : (
                <EmptyState title="未返回依赖详情" description="后端健康接口没有返回依赖明细。" icon={<Activity size={32} />} />
              )}
              <p className="page-description system-check-meta">本次检查耗时 {Math.round(snapshot.elapsedMs)} ms，检查时间 {snapshot.checkedAt.toLocaleString("zh-CN")}。</p>
            </>
          ) : null}
        </section>
      ) : (
        <EmptyState title="无系统状态权限" description="当前账号不能查看服务运行状态。" icon={<Activity size={32} />} />
      )}
    </div>
  );
}

function formatDependencyName(name: string): string {
  const aliases: Record<string, string> = {
    elasticsearch: "Elasticsearch", milvus: "Milvus", embedding: "Embedding", mineru: "MinerU",
    rerank: "Rerank", redis: "Redis", worker: "Worker", sqlite: "SQLite", filesystem: "文件系统",
  };
  return aliases[name] ?? name.replaceAll("_", " ");
}
