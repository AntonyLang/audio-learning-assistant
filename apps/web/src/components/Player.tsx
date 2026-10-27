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
  const [syncError, setSyncError] = useState(false);

  useEffect(() => {
    const element = audioRef.current;
    if (!element) return;
    let active = true;
    let ready = false;
    let position = 0;
    let lastSample = 0;
    let lastWall = performance.now();
    let intervals: number[][] = [];
    const record = (start: number, end: number, kind: string) => {
      seq.current += 1;
      void api.playback(episodeId, sessionId.current, seq.current, Math.round(start), Math.round(end), kind)
        .then(() => { if (active) setSyncError(false); })
        .catch(() => { if (active) setSyncError(true); });
    };
    const flush = () => {
      if (!ready) return;
      if (lastSample > lastReported.current) {
        record(lastReported.current, lastSample, "progress");
        intervals.push([lastReported.current, lastSample]);
        intervals.sort((a, b) => a[0] - b[0]);
        const merged: number[][] = [];
        for (const [start, end] of intervals) {
          const tail = merged[merged.length - 1];
          if (tail && start <= tail[1]) tail[1] = Math.max(tail[1], end);
          else merged.push([start, end]);
        }
        intervals = merged;
        if (active) setListenedMs(merged.reduce((sum, [start, end]) => sum + end - start, 0));
      }
      lastReported.current = lastSample;
    };
    const restore = () => {
      if (!ready || !Number.isFinite(element.duration)) return;
      // A completed episode starts over; otherwise continue from the last device.
      element.currentTime = position < element.duration * 1000 - 1500 ? position / 1000 : 0;
      lastSample = lastReported.current = element.currentTime * 1000;
      element.removeEventListener("loadedmetadata", restore);
    };
    void api.playbackSummary(episodeId).then((summary) => {
      if (!active) return;
      intervals = summary.merged_intervals;
      position = summary.position_ms;
      setListenedMs(summary.listened_ms);
      ready = true;
      if (element.paused) restore();
      else lastSample = lastReported.current = element.currentTime * 1000;
    }).catch(() => { ready = true; if (active) setSyncError(true); });
    const onTimeUpdate = () => {
      if (!ready || element.paused || element.seeking) return;
      const current = element.currentTime * 1000;
      const now = performance.now();
      if (current < lastSample || current - lastSample > (now - lastWall) * element.playbackRate + 1000) {
        flush();
        lastReported.current = current;
      }
      lastSample = current;
      lastWall = now;
      if (current - lastReported.current >= 5000) flush();
    };
    const onPlay = () => {
      lastSample = lastReported.current = element.currentTime * 1000;
      lastWall = performance.now();
    };
    const onSeeking = () => { flush(); };
    const checkpoint = () => {
      flush();
      if (ready) {
        const current = element.currentTime * 1000;
        record(current, current, "position");
      }
    };
    const onSeeked = () => { onPlay(); checkpoint(); };
    const onPause = () => {
      if (!element.seeking && element.currentTime * 1000 - lastSample < 1000) {
        lastSample = element.currentTime * 1000;
      }
      checkpoint();
    };
    element.addEventListener("loadedmetadata", restore);
    element.addEventListener("play", onPlay);
    element.addEventListener("timeupdate", onTimeUpdate);
    element.addEventListener("seeking", onSeeking);
    element.addEventListener("seeked", onSeeked);
    element.addEventListener("pause", onPause);
    element.addEventListener("ended", onPause);
    window.addEventListener("pagehide", checkpoint);
    return () => {
      active = false;
      checkpoint();
      element.removeEventListener("loadedmetadata", restore);
      element.removeEventListener("play", onPlay);
      element.removeEventListener("timeupdate", onTimeUpdate);
      element.removeEventListener("seeking", onSeeking);
      element.removeEventListener("seeked", onSeeked);
      element.removeEventListener("pause", onPause);
      element.removeEventListener("ended", onPause);
      window.removeEventListener("pagehide", checkpoint);
    };
  }, [episodeId]);

  return (
    <div className="player">
      <audio ref={audioRef} controls preload="metadata" src={src} />
      <div className="player-meta">
        <span className="muted small">
          {durationMs ? `${(durationMs / 1000).toFixed(1)} 秒 · 实测` : "音频准备中"}
        </span>
        {listenedMs > 0 && (
          <span className="muted small">累计已听 {Math.min(100, (listenedMs / (durationMs ?? 1)) * 100).toFixed(0)}%</span>
        )}
        {syncError && <span className="error small">进度暂未同步</span>}
      </div>
    </div>
  );
}
