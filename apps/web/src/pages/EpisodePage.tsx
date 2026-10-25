import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { EpisodeDetail } from "../types";

interface Props {
  id: string;
  onBack: () => void;
}

const REVIEW_LABEL: Record<string, string> = {
  SUPPORTED: "已核源",
  PARTIAL: "部分支持",
  CONTRADICTED: "与原文冲突",
  NOT_FOUND: "未找到依据",
};

export default function EpisodePage({ id, onBack }: Props) {
  const [episode, setEpisode] = useState<EpisodeDetail | null>(null);
  const [error, setError] = useState("");
  const [action, setAction] = useState("");

  const refresh = useCallback(async () => {
    try {
      setEpisode(await api.episode(id));
      setError("");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  }, [id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const sendFeedback = async (kind: string) => {
    setAction("提交反馈…");
    try {
      const result = await api.feedback(id, kind, "");
      setAction(
        result.action === "follow_up_created"
          ? "已创建后续候选节目，回到收件箱可看到进度"
          : "已记录，偏好将在下一期生效",
      );
    } catch (err) {
      setAction(err instanceof ApiError ? err.detail : String(err));
    }
  };

  if (error) {
    return (
      <div className="page">
        <div className="error">{error}</div>
        <button className="ghost" onClick={onBack}>
          返回
        </button>
      </div>
    );
  }
  if (!episode) return <div className="boot">加载中…</div>;

  const claims = episode.script?.claims ?? [];
  const cards = new Map(episode.evidence.map((card) => [card.chunk_id, card]));

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <button className="link" onClick={onBack}>
            ← 返回
          </button>
          <h1>{episode.question || "未命名节目"}</h1>
          <p className="muted">
            {episode.published ? "已发布" : "未发布"} ·{" "}
            {episode.duration_ms ? `${(episode.duration_ms / 1000).toFixed(1)} 秒` : "时长未知"} ·{" "}
            {episode.revision?.status ?? "无版本"} · GUID {episode.guid.slice(0, 8)}
          </p>
        </div>
      </header>

      {episode.audio?.web_url && episode.published && (
        <section className="card">
          <audio controls preload="metadata" src={episode.audio.web_url} style={{ width: "100%" }} />
          <div className="muted small">
            字节 SHA-256 {episode.audio.sha256?.slice(0, 16)}… · 单条上限 300 秒
          </div>
        </section>
      )}

      {episode.script?.one_line_answer && (
        <section className="card answer">
          <div className="label">一句话答案</div>
          <p>{episode.script.one_line_answer}</p>
        </section>
      )}

      <section className="list">
        {claims.map((claim) => {
          const card = claim.evidence[0] ? cards.get(claim.evidence[0].chunk_id) : undefined;
          return (
            <article className="claim" key={claim.claim_id}>
              <div className="claim-head">
                <span className={`badge review-${claim.review}`}>
                  {REVIEW_LABEL[claim.review] ?? claim.review}
                </span>
                <span className="muted small">
                  {claim.kind}
                  {claim.critical ? " · 关键陈述" : ""}
                </span>
              </div>
              <p className="claim-text">{claim.text}</p>
              {claim.evidence.map((ref) => (
                <blockquote key={`${ref.chunk_id}-${ref.span_start}`}>
                  <span className="quote">{ref.quote}</span>
                  <footer className="muted small">
                    {card?.source_title ?? "来源"} · {card?.section_path || "正文"} · 字符{" "}
                    {ref.span_start}–{ref.span_end}
                    {card?.page_no ? ` · 第 ${card.page_no} 页` : ""}
                  </footer>
                </blockquote>
              ))}
            </article>
          );
        })}
      </section>

      <section className="card">
        <div className="label">反馈</div>
        <div className="feedback">
          <button className="ghost" onClick={() => void sendFeedback("shallower")}>
            浅一点
          </button>
          <button className="ghost" onClick={() => void sendFeedback("example")}>
            再举例
          </button>
          <button className="ghost" onClick={() => void sendFeedback("deeper")}>
            继续深入
          </button>
          <button className="ghost" onClick={() => void sendFeedback("less_like_this")}>
            少推荐这个方向
          </button>
        </div>
        {action && <div className="muted small">{action}</div>}
      </section>
    </div>
  );
}
