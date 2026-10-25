import { useEffect, useRef, useState } from "react";
import { api } from "../api";

interface Props {
  episodeId: string;
  src: string;
  durationMs: number | null;
}

export default function Player({ episodeId, src, durationMs }: Props) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const sessionId = useRef<string>(crypto.randomUUID());
  const seq = useRef(0);
  const lastReported = useRef(0);
  const [listenedMs, setListenedMs] = useState(0);

  useEffect(() => {
    const element = audioRef.current;
    if (!element) return;
    const onTimeUpdate = () => {
      const current = element.currentTime * 1000;
      if (current - lastReported.current < 5000) return;
      const start = lastReported.current;
      lastReported.current = current;
      seq.current += 1;
      setListenedMs((value) => value + Math.max(0, current - start));
      void api
        .playback(episodeId, sessionId.current, seq.current, Math.round(start), Math.round(current))
        .catch(() => undefined);
    };
    element.addEventListener("timeupdate", onTimeUpdate);
    return () => element.removeEventListener("timeupdate", onTimeUpdate);
  }, [episodeId]);

  return (
    <div className="player">
      <audio ref={audioRef} controls preload="metadata" src={src} />
      <div className="player-meta">
        <span className="muted small">
          {durationMs ? `${(durationMs / 1000).toFixed(1)} 秒 · 实测` : "音频准备中"}
        </span>
        {listenedMs > 0 && (
          <span className="muted small">本次已听 {Math.min(100, (listenedMs / (durationMs ?? 1)) * 100).toFixed(0)}%</span>
        )}
      </div>
    </div>
  );
}
