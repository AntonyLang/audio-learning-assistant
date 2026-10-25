import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import RunProgress from "../components/RunProgress";
import type { Created, RunSnapshot, SourceCard } from "../types";

const QUALITY_LABEL: Record<string, string> = {
  FULL: "读取完整",
  PARTIAL: "部分读取",
  NEEDS_INPUT: "需要补充",
  FAILED: "读取失败",
};

interface Props {
  onOpenEpisode: (id: string) => void;
}

export default function Inbox({ onOpenEpisode }: Props) {
  const [sources, setSources] = useState<SourceCard[]>([]);
  const [mode, setMode] = useState<"text" | "url" | "pdf">("text");
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [question, setQuestion] = useState("");
  const [activeRun, setActiveRun] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingSource, setPendingSource] = useState<string | null>(null);
  const [pendingEpisode, setPendingEpisode] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await api.sources();
      setSources(result.items);
    } catch (error) {
      setMessage(error instanceof ApiError ? error.detail : String(error));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const submitSource = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      let created: Created;
      if (mode === "text") {
        created = await api.createTextSource(text, title || "粘贴的文本");
      } else if (mode === "url") {
        created = await api.createUrlSource(url, title, question);
      } else {
        if (!file) throw new Error("请选择一个 PDF 文件");
        created = await api.uploadPdf(file, title || file.name);
      }
      setMessage(`已受理：${created.run_id.slice(0, 8)}… 正在读取`);
      setActiveRun(created.run_id);
      setPendingSource(created.source_id ?? null);
    } catch (error) {
      setMessage(error instanceof ApiError ? error.detail : String(error));
    } finally {
      setBusy(false);
    }
  };

  const createEpisode = async (sourceId: string) => {
    setBusy(true);
    setMessage("");
    try {
      const created = await api.createEpisode(sourceId, question);
      setActiveRun(created.run_id);
      setPendingEpisode(created.episode_id ?? null);
      setMessage(
        created.deferred
          ? "今日预算已用完，任务进入 DEFERRED，明天会重新安排。"
          : "已开始生成，预算已预留。",
      );
    } catch (error) {
      if (error instanceof ApiError && error.detail.startsWith("source_partial")) {
        const confirmed = window.confirm("这份资料只读到部分正文，是否只用已提取部分生成？");
        if (confirmed) {
          try {
            const created = await api.createEpisode(sourceId, question, true);
            setActiveRun(created.run_id);
          } catch (inner) {
            setMessage(String(inner));
          }
        }
      } else {
        setMessage(error instanceof ApiError ? error.detail : String(error));
      }
    } finally {
      setBusy(false);
    }
  };

  const onRunFinished = async (snapshot: RunSnapshot) => {
    if (snapshot.state === "SUCCEEDED" && pendingSource) {
      setMessage("准备完成，正在发布到今日清单…");
      setTimeout(() => void refresh(), 1500);
    }
    await refresh();
  };

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>收件箱</h1>
          <p className="muted">投递 URL、文本或 PDF。RSS 只提供候选，不会自动合成。</p>
        </div>
        <button className="ghost" onClick={() => void refresh()}>
          刷新
        </button>
      </header>

      <section className="card">
        <div className="tabs">
          <button className={mode === "text" ? "active" : ""} onClick={() => setMode("text")}>
            粘贴文本
          </button>
          <button className={mode === "url" ? "active" : ""} onClick={() => setMode("url")}>
            网页 URL
          </button>
          <button className={mode === "pdf" ? "active" : ""} onClick={() => setMode("pdf")}>
            PDF 上传
          </button>
        </div>
        <form onSubmit={submitSource} className="form">
          <label>
            标题（可选）
            <input value={title} onChange={(event) => setTitle(event.target.value)} />
          </label>
          {mode === "text" && (
            <label>
              正文
              <textarea
                rows={8}
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder="粘贴文章正文，系统会保留快照与可回查的字符范围。"
              />
            </label>
          )}
          {mode === "url" && (
            <label>
              链接
              <input
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://example.com/article"
              />
            </label>
          )}
          {mode === "pdf" && (
            <label>
              文件
              <input
                type="file"
                accept="application/pdf"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </label>
          )}
          <label>
            本期问题（可选）
            <input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="例：调用超时为什么不能断定远端没有完成？"
            />
          </label>
          <div className="actions">
            <button className="primary" type="submit" disabled={busy}>
              {busy ? "提交中…" : "投递"}
            </button>
            <span className="muted small">{message}</span>
          </div>
        </form>
      </section>

      {activeRun && pendingEpisode && (
        <button className="ghost" onClick={() => onOpenEpisode(pendingEpisode)}>
          查看节目详情
        </button>
      )}

      {activeRun && (
        <RunProgress
          runId={activeRun}
          onFinished={(snapshot) => {
            void onRunFinished(snapshot);
            if (snapshot.state === "SUCCEEDED") {
              setTimeout(() => void refresh(), 2000);
            }
          }}
        />
      )}

      <section className="list">
        {sources.map((source) => (
          <article className="row" key={source.source_id}>
            <div>
              <div className="row-title">{source.title || source.canonical_url || "未命名资料"}</div>
              <div className="muted small">
                {source.kind} ·{" "}
                {source.version
                  ? `${QUALITY_LABEL[source.version.quality] ?? source.version.quality} · ${source.version.chars} 字`
                  : "尚未读取"}
              </div>
            </div>
            <div className="row-actions">
              <button
                className="ghost"
                disabled={!source.ready || busy}
                onClick={() => void createEpisode(source.source_id)}
              >
                生成节目
              </button>
            </div>
          </article>
        ))}
        {sources.length === 0 && <div className="empty">还没有资料。先投递一篇试试。</div>}
      </section>
    </div>
  );
}
