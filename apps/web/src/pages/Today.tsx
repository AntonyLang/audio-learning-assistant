import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { Today } from "../types";

interface Props {
  onOpenEpisode: (id: string) => void;
}

function formatSeconds(ms: number | null): string {
  if (!ms) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

export default function TodayPage({ onOpenEpisode }: Props) {
  const [today, setToday] = useState<Today | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setToday(await api.today());
      setError("");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const ratio = today ? Math.min(100, (today.total_ms / today.budget_ms) * 100) : 0;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>今日收听</h1>
          <p className="muted">
            {today
              ? `${today.local_date} · ${today.timezone} · 已安排 ${formatSeconds(today.total_ms)} / ${formatSeconds(today.budget_ms)}`
              : "正在读取计划…"}
          </p>
        </div>
        <button className="ghost" onClick={() => void refresh()}>
          刷新
        </button>
      </header>

      {error && <div className="error">{error}</div>}

      <section className="card">
        <div className="budget">
          <div className="budget-bar">
            <div className="budget-fill" style={{ width: `${ratio}%` }} />
          </div>
          <div className="budget-legend">
            <span>已发布 {formatSeconds(today?.available_ms ?? 0)}</span>
            <span>预留 {formatSeconds(today?.reserved_ms ?? 0)}</span>
            <span>剩余 {formatSeconds((today?.budget_ms ?? 0) - (today?.total_ms ?? 0))}</span>
          </div>
        </div>
        <p className="muted small">
          预算是播放时间，不是任务数量。已安排过的节目不会因为听完或刷新页面而返还额度。
        </p>
      </section>

      <section className="list">
        {(today?.items ?? []).map((item) => (
          <article className="row" key={item.episode_id}>
            <div className="row-index">{String(item.position).padStart(2, "0")}</div>
            <div className="row-main">
              <div className="row-title">{item.question || "未命名节目"}</div>
              <div className="muted small">
                {item.state === "AVAILABLE"
                  ? `可播放 · ${formatSeconds(item.duration_ms)}`
                  : item.state === "RESERVED"
                    ? `生成中 · 预留 ${formatSeconds(item.reserved_ms)}`
                    : item.state === "DEFERRED"
                      ? "预算不足，已延后"
                      : "已释放"}
              </div>
            </div>
            <div className="row-actions">
              <button
                className="ghost"
                onClick={() => onOpenEpisode(item.episode_id)}
                disabled={!item.published}
              >
                详情
              </button>
            </div>
          </article>
        ))}
        {(today?.items ?? []).length === 0 && (
          <div className="empty">今天还没有节目。去收件箱投递一篇资料。</div>
        )}
      </section>
    </div>
  );
}
