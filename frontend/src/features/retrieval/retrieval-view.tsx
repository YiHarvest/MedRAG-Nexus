"use client";

import { ChevronDown, Search } from "@carbon/icons-react";
import {
  Button,
  InlineLoading,
  InlineNotification,
  NumberInput,
  TextArea,
} from "@carbon/react";
import { type FormEvent } from "react";
import { useRetrievalMemory } from "@/components/app-memory";
import { EmptyState } from "@/components/states";
import { useWorkspace, WorkspaceBar } from "@/components/workspace";
import { describeWebUiError, retrieveWebUiKnowledge } from "@/lib/webui-api";
import type { RetrievalScores } from "@/lib/types";

export function RetrievalView() {
  const { workspace } = useWorkspace();
  const {
    query,
    setQuery,
    topK,
    setTopK,
    result,
    setResult,
    elapsed,
    setElapsed,
    loading,
    setLoading,
    error,
    setError,
  } = useRetrievalMemory();

  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim() || !workspace.userId || !workspace.workspaceId) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setElapsed(null);
    const startedAt = performance.now();
    try {
      const response = await retrieveWebUiKnowledge({
        workspace_id: workspace.workspaceId,
        query: query.trim(),
        top_k: topK,
      });
      setResult(response);
    } catch (caught) {
      setError(describeWebUiError(caught));
    } finally {
      setElapsed(performance.now() - startedAt);
      setLoading(false);
    }
  }

  return (
    <div className="page retrieval-page">
      <section className="retrieval-compose" aria-label="知识库检索" aria-busy={loading}>
        <div className="retrieval-workspace-row">
          <WorkspaceBar />
        </div>

        <form className="retrieval-form" onSubmit={handleSearch}>
          <TextArea
            id="retrieval-query"
            labelText="问题"
            placeholder="输入你想从知识库中查找的问题"
            rows={3}
            value={query}
            disabled={loading}
            onChange={(event) => setQuery(event.target.value)}
          />
          <footer className="retrieval-form-footer">
            <div className="retrieval-top-k">
              <NumberInput
                id="retrieval-top-k"
                label="返回数量"
                min={1}
                max={50}
                value={topK}
                disabled={loading}
                onChange={(_event, state) => setTopK(Number(state.value))}
              />
              <span>最多返回 50 个知识片段</span>
            </div>
            <Button
              type="submit"
              disabled={loading || !query.trim() || !workspace.userId || !workspace.workspaceId}
              renderIcon={Search}
            >
              {loading ? "检索中" : "检索"}
            </Button>
          </footer>
        </form>
      </section>

      {error ? (
        <InlineNotification
          kind="error"
          title="检索失败"
          subtitle={error}
          lowContrast
          onCloseButtonClick={() => setError(null)}
        />
      ) : null}

      <section className="retrieval-output" aria-live="polite">
        {loading ? (
          <div className="retrieval-loading">
            <InlineLoading description="正在检索关键词与向量索引" />
            <p>慢速通道会在 3 秒后自动降级，不会一直等待。</p>
          </div>
        ) : result ? (
          <>
            <header className="retrieval-results-header">
              <div>
                <h2>{result.count ? `${result.count} 条结果` : "没有匹配结果"}</h2>
                <p>“{result.query}”</p>
              </div>
              <dl className="retrieval-summary">
                <div><dt>耗时</dt><dd>{elapsed === null ? "暂无" : `${Math.round(elapsed)} ms`}</dd></div>
                <div><dt>Top K</dt><dd>{result.top_k}</dd></div>
              </dl>
            </header>

            {result.warnings.length ? (
              <div className="retrieval-warnings">
                {result.warnings.map((warning) => (
                  <InlineNotification
                    key={`${warning.code}-${warning.message}`}
                    kind="warning"
                    lowContrast
                    hideCloseButton
                    title="部分检索通道已降级"
                    subtitle={warning.message}
                  />
                ))}
              </div>
            ) : null}

            {result.items.length === 0 ? (
              <EmptyState title="没有找到相关内容" description="换一种说法，或确认内容已经完成入库。" />
            ) : (
              <div className="retrieval-results-list">
                {result.items.map((item) => {
                  const primaryScore = getPrimaryScore(item.scores);
                  return (
                    <details className="retrieval-result" key={item.chunk_id}>
                      <summary className="retrieval-result-summary">
                        <span className="retrieval-result-rank">{item.rank}</span>
                        <span className="retrieval-result-title">
                          <strong>{item.file_name ?? "文本知识"}</strong>
                          <small>
                            {item.section ? `${item.section} · ` : ""}
                            {item.page_number ? `第 ${item.page_number} 页 · ` : ""}
                            {item.matched_by.map(channelLabel).join(" + ")}
                          </small>
                        </span>
                        <span className="retrieval-source-type">{item.source_type === "file" ? "文件" : "文本"}</span>
                        <span className="retrieval-primary-score">
                          <small>{primaryScore.label}</small>
                          <strong>{primaryScore.value}</strong>
                        </span>
                        <ChevronDown className="retrieval-result-chevron" size={18} aria-hidden="true" />
                      </summary>
                      <div className="retrieval-result-panel">
                        <p className="retrieval-result-content">{item.content}</p>
                        <div className="retrieval-score-grid">
                          <Score label="Vector" value={item.scores.vector} />
                          <Score label="BM25" value={item.scores.bm25} />
                          <Score label="RRF" value={item.scores.rrf} />
                          <Score label="Rerank" value={item.scores.rerank} />
                        </div>
                        <div className="retrieval-chunk-meta">
                          <span>Chunk ID</span>
                          <code>{item.chunk_id}</code>
                        </div>
                      </div>
                    </details>
                  );
                })}
              </div>
            )}
          </>
        ) : (
          <EmptyState
            title="输入问题开始检索"
            description="检索结果会显示来源、命中方式和召回分数。"
            icon={<Search size={28} />}
          />
        )}
      </section>
    </div>
  );
}

function Score({ label, value }: { label: string; value?: number | null }) {
  return (
    <span>
      <small>{label}</small>
      <strong>{value === null || value === undefined ? "暂无" : value.toFixed(4)}</strong>
    </span>
  );
}

function getPrimaryScore(scores: RetrievalScores): { label: string; value: string } {
  const candidates = [
    ["Rerank", scores.rerank],
    ["RRF", scores.rrf],
    ["Vector", scores.vector],
    ["BM25", scores.bm25],
  ] as const;
  const score = candidates.find(([, value]) => value !== null && value !== undefined);
  return score ? { label: score[0], value: score[1]!.toFixed(4) } : { label: "分数", value: "暂无" };
}

function channelLabel(channel: "vector" | "bm25") {
  return channel === "vector" ? "向量" : "关键词";
}
