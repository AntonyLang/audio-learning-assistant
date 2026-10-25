import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { RunSnapshot } from "../types";

const PHASES = [
  { key: "RESEARCH", label: "研究" },
  { key: "SCRIPT", label: "写稿" },
  { key: "TTS", label: "合成" },
  { key: "AUDIO_CHECK", label: "测时长" },
  { key: "PUBLISH", label: "发布" },
];

interface Props {
  runId: string;
  onFinished?: (snapshot: RunSnapshot) => void;
}

export default function RunProgress({ runId, onFinished }: Props) {
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [events, setEvents] = useState<string[]>([]);
  const finishedRef = useRef(false);

  useEffect(() => {
    finishedRef.current = false;
    setEvents([]);
    const source = new EventSource(`/v1/runs/${runId}/events`);
    const terminal = new Set(["SUCCEEDED", "FAILED", "WAITING_REVIEW", "CANCELLED"]);

    const refresh = async () => {
      try {
        const next = await api.run(runId);
        setSnapshot(next);
        if (terminal.has(next.state) && !finishedRef.current) {
          finishedRef.current = true;
          source.close();
          onFinished?.(next);
        }
      } catch {
        /* keep the stream alive; a transient failure is retried by the browser */
      }
    };

    source.onmessage = () => void refresh();
    for (const type of ["run.phase", "script.ready", "audio.measured", "episode.published", "run.failed"]) {
      source.addEventListener(type, (event) => {
        const data = JSON.parse((event as MessageEvent).data) as Record<string, unknown>;
        const label =
          type === "run.phase" ? `${data.phase ?? ""}` : `${type} ${data.reason ?? ""}`.trim();
        setEvents((current) => [...current.slice(-5), label]);
        void refresh();
      });
    }
    source.addEventListener("stream.end", () => {
      source.close();
      void refresh();
    });
    source.onerror = () => {
      void refresh();
    };
    void refresh();
    return () => source.close();
  }, [runId, onFinished]);

  const phaseIndex = snapshot?.phase ? PHASES.findIndex((entry) => entry.key === snapshot.phase) : -1;

  return (
    <div className="progress-card">
      <div className="progress-head">
        <strong>生成进度</strong>
        <span className={`badge state-${snapshot?.state ?? "QUEUED"}`}>
          {snapshot?.state ?? "QUEUED"}
        </span>
      </div>
      <ol className="phases">
        {PHASES.map((phase, index) => (
          <li
            key={phase.key}
            className={
              snapshot?.state === "SUCCEEDED" || (phaseIndex >= 0 && index < phaseIndex)
                ? "done"
                : phaseIndex === index
                  ? "current"
                  : ""
            }
          >
            {phase.label}
          </li>
        ))}
      </ol>
      {events.length > 0 && (
        <ul className="event-log">
          {events.map((event, index) => (
            <li key={`${event}-${index}`}>{event}</li>
          ))}
        </ul>
      )}
      {snapshot?.revision?.duration_ms ? (
        <div className="muted small">
          实测时长 {(snapshot.revision.duration_ms / 1000).toFixed(1)} 秒 · attempt {snapshot.attempt_no}
        </div>
      ) : (
        <div className="muted small">单条上限 300 秒，按实测时长验收。</div>
      )}
      {snapshot?.last_error && <div className="error small">{snapshot.last_error}</div>}
      {snapshot?.stop_reason && (
        <div className="muted small">停止原因：{snapshot.stop_reason}</div>
      )}
    </div>
  );
}
