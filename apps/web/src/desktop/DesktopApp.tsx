import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { audioUrl, Detail, Entry, request, Status } from "./api";
import "./desktop.css";

const STOPPED = new Set(["failed", "interrupted", "cancelled", "waiting_tts"]);
const TTS_LABELS: Record<string, string> = {
  stopped: "TTS 已停止", starting: "TTS 正在加载", running: "TTS 已就绪", stopping: "TTS 正在停止", error: "TTS 启动失败",
};
function duration(seconds: number) {
  return `${Math.floor(seconds / 60)}:${Math.floor(seconds % 60).toString().padStart(2, "0")}`;
}
function Icon({ name }: { name: "folder" | "arrow" | "attach" | "close" }) {
  return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
    {name === "folder" ? <path d="M3 7h7l2-3h8v16H3V7Zm0 3h17" /> :
      name === "arrow" ? <path d="M5 12h14m-5-5 5 5-5 5" /> :
        name === "attach" ? <path d="m8 13 7-7a3 3 0 0 1 4 4L9 20a5 5 0 0 1-7-7L13 2m-8 13L16 4" /> :
          <path d="m6 6 12 12M18 6 6 18" />}
  </svg>;
}

export default function DesktopApp() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [loadError, setLoadError] = useState("");
  const [toast, setToast] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [ttsBusy, setTtsBusy] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const settingsRef = useRef<HTMLDialogElement>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [nextEntries, nextStatus] = await Promise.all([
      request<Entry[]>("/entries", { signal }), request<Status>("/status", { signal }),
    ]);
    setEntries(nextEntries); setStatus(nextStatus); setLoadError("");
  }, []);

  useEffect(() => {
    document.body.classList.add("desktop-body");
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { await refresh(controller.signal); }
      catch (error) { if (!controller.signal.aborted) setLoadError((error as Error).message); }
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), document.hidden ? 10000 : 2500);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); document.body.classList.remove("desktop-body"); };
  }, [refresh]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 7000);
    return () => clearTimeout(timer);
  }, [toast]);

  const selectedUpdate = entries.find((entry) => entry.id === selected)?.updated_at;
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    void request<Detail>(`/entries/${selected}`, { signal: controller.signal })
      .then(setDetail).catch((error: Error) => { if (!controller.signal.aborted) setToast(error.message); });
    return () => controller.abort();
  }, [selected, selectedUpdate]);

  useEffect(() => {
    if (selected) dialogRef.current?.showModal();
    else dialogRef.current?.close();
  }, [selected]);
  useEffect(() => {
    if (settingsOpen) settingsRef.current?.showModal();
    else settingsRef.current?.close();
  }, [settingsOpen]);

  const addFiles = (incoming: File[]) => {
    setFiles((current) => [...current, ...incoming.filter((file) => !current.some((item) =>
      item.name === file.name && item.size === file.size && item.lastModified === file.lastModified))]);
  };
  const act = async (path: string, method = "POST") => {
    try { await request(path, { method }); await refresh(); }
    catch (error) { setToast((error as Error).message); }
  };
  const controlTts = async (action: "start" | "stop") => {
    setTtsBusy(true);
    try { await request(`/tts/${action}`, { method: "POST" }); await refresh(); }
    catch (error) { setToast((error as Error).message); }
    finally { setTtsBusy(false); }
  };
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    form.set("web_enabled", form.get("web_enabled") === "true" ? "true" : "false");
    const material = String(form.get("material") || "");
    const lines = material.split("\n");
    form.delete("material");
    form.set("urls", lines.filter((line) => /^https?:\/\/\S+$/.test(line.trim())).join("\n"));
    form.set("text", lines.filter((line) => !/^https?:\/\/\S+$/.test(line.trim())).join("\n"));
    files.forEach((file) => form.append("files", file));
    setSubmitting(true);
    try {
      await request<Entry>("/entries", { method: "POST", body: form });
      formRef.current?.reset(); setFiles([]); await refresh();
      setToast("资料已投递，开始处理。");
    } catch (error) { setToast((error as Error).message); }
    finally { setSubmitting(false); }
  };
  const openDetail = (id: string) => { setDetail(null); setSelected(id); };
  const reveal = async (id: string) => {
    try { await request(`/entries/${id}/reveal`, { method: "POST" }); setToast("已在文件资源管理器中定位音频。"); }
    catch (error) { setToast((error as Error).message); }
  };
  const active = entries.filter((entry) => entry.status !== "completed");
  const completed = entries.filter((entry) => entry.status === "completed");
  const ttsState = status?.tts.state || "stopped";

  return <div className="desktop-app">
    <header className="desktop-header">
      <a className="desktop-brand" href="/desktop/" aria-label="音频学习助手首页">
        <span className="desktop-logo" aria-hidden="true"><i /><i /><i /><i /><i /></span>
        <span>音频学习助手<small>本地工作台</small></span>
      </a>
      <div className="desktop-controls">
        <button className="cpa-indicator" onClick={() => setSettingsOpen(true)} title="文本模型设置">
          <span className={`status-dot ${status?.cpa.connected ? "is-on" : ""}`} />
          CPA {status?.cpa.connected ? "已连接" : "未连接"}
        </button>
        <span className="desktop-control-divider" />
        <span className="tts-indicator" title={status?.tts.error || status?.tts.model}>
          <span className={`status-dot ${ttsState === "running" ? "is-on" : ttsState === "starting" ? "is-busy" : ""}`} />
          {TTS_LABELS[ttsState]}
        </span>
        <button className="desktop-button small-button" disabled={ttsBusy || ttsState === "starting" || ttsState === "stopping"}
          onClick={() => void controlTts(ttsState === "running" ? "stop" : "start")}>
          {ttsState === "running" ? "停止模型" : "启动模型"}
        </button>
        {ttsState === "starting" && <button className="desktop-text-button" disabled={ttsBusy} onClick={() => void controlTts("stop")}>取消加载</button>}
      </div>
    </header>
    {loadError && <div className="desktop-banner" role="alert">本地服务未响应：{loadError}</div>}
    {status?.tts.error && <div className="desktop-banner" role="alert">{status.tts.error}</div>}
    {status && !status.cpa.connected && <div className="desktop-banner">请手动启动 CPA。资料可以先投递，连接后点击条目的“继续”。</div>}
    <main className="desktop-workspace">
      <section className="outbox-panel" aria-labelledby="outbox-title">
        <div className="desktop-section-title"><h1 id="outbox-title">资料投递 <span>Outbox</span></h1></div>
        <form ref={formRef} onSubmit={(event) => void submit(event)} className="desktop-composer">
          <label className="sr-only" htmlFor="entry-title">学习主题</label>
          <input id="entry-title" name="title" className="desktop-title-input" placeholder="这次想学什么？（可选）" maxLength={120} />
          <label className="sr-only" htmlFor="entry-material">资料正文与链接</label>
          <textarea id="entry-material" name="material" rows={5} placeholder={"粘贴资料正文，或网页 / PDF 链接\n多个链接请每行一个"} />
          <div className={`desktop-dropzone ${dragging ? "is-dragging" : ""}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles([...event.dataTransfer.files]); }}>
            <input ref={fileRef} className="sr-only" type="file" multiple id="entry-files" accept={status?.formats.join(",")} onChange={(event) => { addFiles([...event.target.files || []]); event.target.value = ""; }} />
            <button className="desktop-attach" type="button" onClick={() => fileRef.current?.click()}><Icon name="attach" /> 添加多个文件</button>
            <span>或拖到这里</span>
            <small>PDF、图片、Word、PPT、文本 · 每个文件 ≤ 30MB</small>
          </div>
          {files.length > 0 && <ul className="desktop-file-list">{files.map((file, index) => <li key={`${file.name}-${index}`}>
            <span title={file.name}>{file.name}</span><small>{(file.size / 1024).toFixed(0)} KB</small>
            <button type="button" className="desktop-icon-button" aria-label={`移除 ${file.name}`} onClick={() => setFiles((current) => current.filter((_, i) => i !== index))}><Icon name="close" /></button>
          </li>)}</ul>}
          <details className="desktop-options"><summary>学习要求与时长</summary>
            <label htmlFor="entry-instructions">希望重点解释什么？</label>
            <textarea id="entry-instructions" name="instructions" rows={2} placeholder="例如：用生活中的例子解释，面向初学者" maxLength={5000} />
            <label className="desktop-inline-field" htmlFor="entry-duration">目标时长
              <select id="entry-duration" name="target_seconds" defaultValue="180"><option value="30">30 秒试音</option><option value="60">约 1 分钟</option><option value="180">约 3 分钟</option><option value="300">约 5 分钟</option></select>
            </label>
          </details>
          <div className="desktop-submit-row">
            <label className="desktop-check"><input type="checkbox" name="web_enabled" value="true" defaultChecked /> 联网补充与核实</label>
            <button type="submit" className="desktop-button primary" disabled={submitting}>{submitting ? "正在投递…" : "投递并生成"}<Icon name="arrow" /></button>
          </div>
        </form>
        <div className="desktop-list-heading">已投递 <span>{entries.length}</span></div>
        <div className="desktop-outbox-list">
          {entries.length === 0 ? <p className="desktop-muted desktop-no-entries">一个条目可以放入多份资料，合成一段音频。</p> : entries.map((entry) => <button className="desktop-outbox-item" key={entry.id} onClick={() => openDetail(entry.id)}>
            <span className="desktop-outbox-name">{entry.title}</span>
            <span className="desktop-outbox-meta">{entry.source_count} 份资料 <i>·</i> {entry.stage}</span>
          </button>)}
        </div>
      </section>
      <section className="audio-panel" aria-labelledby="audio-title">
        <div className="desktop-section-title"><h2 id="audio-title">音频</h2><span className="desktop-muted">生成后保存在这台电脑</span></div>
        {entries.length === 0 && <div className="desktop-empty"><div className="desktop-empty-waves" aria-hidden="true">〰</div><h3>把想读的，变成可以听的。</h3><p>从左侧投递资料，音频会出现在这里。</p></div>}
        {active.length > 0 && <div className="desktop-audio-group"><h3 className="desktop-group-label">生成中 <span>{active.length}</span></h3>
          {active.map((entry) => <article className="desktop-audio-card" key={entry.id}>
            <div className="desktop-card-top"><button className="desktop-card-title" onClick={() => openDetail(entry.id)}>{entry.title}</button>
              <span className={`desktop-state-label ${entry.status === "failed" ? "has-error" : ""}`}>{entry.stage}</span></div>
            {entry.status === "voicing" && <div className="desktop-progress"><progress value={entry.segment_done} max={entry.segment_total || 1} /><span>{entry.segment_done} / {entry.segment_total} 段</span></div>}
            {entry.status === "researching" || entry.status === "parsing" ? <div className="desktop-working"><span />{entry.status === "parsing" ? "整理多份资料中的内容" : "检索、阅读与撰写讲稿"}</div> : null}
            {entry.error && <p className="desktop-error">{entry.error}</p>}
            <div className="desktop-card-actions"><button className="desktop-text-button" onClick={() => openDetail(entry.id)}>资料与处理过程</button>
              {STOPPED.has(entry.status) ? <button className="desktop-button small-button" onClick={() => void act(`/entries/${entry.id}/retry`)}>继续</button> :
                <button className="desktop-text-button subdued" onClick={() => void act(`/entries/${entry.id}/cancel`)}>取消</button>}
            </div>
          </article>)}
        </div>}
        {completed.length > 0 && <div className="desktop-audio-group"><h3 className="desktop-group-label">已完成 <span>{completed.length}</span></h3>
          {completed.map((entry) => <article className="desktop-audio-card complete-card" key={entry.id}>
            <div className="desktop-card-top"><button className="desktop-card-title" onClick={() => openDetail(entry.id)}>{entry.title}</button><span className="desktop-muted">{duration(entry.duration_seconds)}</span></div>
            <audio controls preload="none" src={audioUrl(entry.id)} aria-label={`播放：${entry.title}`} />
            <div className="desktop-card-actions"><button className="desktop-text-button" onClick={() => openDetail(entry.id)}>讲稿与来源</button>
              <button className="desktop-text-button" onClick={() => void reveal(entry.id)}><Icon name="folder" />在文件资源管理器打开</button></div>
          </article>)}
        </div>}
      </section>
    </main>
    {toast && <div className="desktop-toast" role="status">{toast}<button className="desktop-icon-button" aria-label="关闭提示" onClick={() => setToast("")}><Icon name="close" /></button></div>}
    <dialog ref={dialogRef} className="desktop-dialog" onClose={() => setSelected(null)}>
      <div className="desktop-dialog-header"><h2>{detail?.title || "正在读取…"}</h2><button className="desktop-icon-button" aria-label="关闭详情" onClick={() => setSelected(null)}><Icon name="close" /></button></div>
      {detail && <div className="desktop-dialog-body">
        <p className="desktop-muted">{detail.stage}{detail.model ? ` · ${detail.model}` : ""}</p>
        {detail.summary && <p>{detail.summary}</p>}
        {detail.script && <section><h3>口播讲稿</h3><p className="desktop-script">{detail.script}</p></section>}
        <section><h3>资料与来源 <span className="desktop-muted">{detail.sources.length}</span></h3>
          {detail.sources.map((source) => <details className="desktop-source" key={source.id}><summary>{source.name}<small>{source.status === "ready" ? `${source.chars} 字` : source.status === "error" ? "解析失败" : "等待解析"}{source.external ? " · 联网来源" : ""}</small></summary>
            {source.location.startsWith("http") && <a href={source.location} target="_blank" rel="noreferrer">打开原文 ↗</a>}
            {source.error ? <p className="desktop-error">{source.error}</p> : <pre>{source.text || "尚未解析"}{source.chars > 1800 ? "\n…（预览仅显示前 1800 字，Agent 可读取全文）" : ""}</pre>}
          </details>)}
        </section>
        <details className="desktop-research-log"><summary>处理过程</summary><ol>{detail.events.map((event) => <li key={event.seq}><time>{new Date(event.created_at).toLocaleTimeString("zh-CN")}</time>{event.message}</li>)}</ol></details>
      </div>}
    </dialog>
    <dialog ref={settingsRef} className="desktop-dialog settings-dialog" onClose={() => setSettingsOpen(false)}>
      <div className="desktop-dialog-header"><h2>文本模型</h2><button className="desktop-icon-button" aria-label="关闭设置" onClick={() => setSettingsOpen(false)}><Icon name="close" /></button></div>
      <div className="desktop-dialog-body"><p>CPA 由你手动启动，应用只连接它。</p><p className="desktop-muted">{status?.cpa_url}</p>
        <label htmlFor="cpa-model">当前模型</label><select id="cpa-model" value={status?.model || ""} disabled={!status?.cpa.connected} onChange={(event) => {
          void request("/model", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: event.target.value }) })
            .then(() => refresh()).catch((error: Error) => setToast(error.message));
        }}>{[...new Set([status?.model || "", ...status?.cpa.models || []])].filter(Boolean).map((model) => <option key={model} value={model}>{model}</option>)}</select>
        <p className="desktop-muted">搜索、网页 / PDF 阅读、论文检索和资料检索已提供给 Agent，无需额外搜索密钥。</p>
      </div>
    </dialog>
  </div>;
}
