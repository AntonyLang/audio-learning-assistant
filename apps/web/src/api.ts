import type {
  Created,
  EpisodeDetail,
  FeedToken,
  RunSnapshot,
  SourceCard,
  Today,
} from "./types";

const CSRF_KEY = "ala.csrf";
let csrf = sessionStorage.getItem(CSRF_KEY) ?? "";

export function setCsrf(value: string) {
  csrf = value;
  sessionStorage.setItem(CSRF_KEY, value);
}

export function clearCsrf() {
  csrf = "";
  sessionStorage.removeItem(CSRF_KEY);
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("content-type", "application/json");
  }
  if (init.method && init.method !== "GET" && csrf) {
    headers.set("x-csrf-token", csrf);
  }
  const response = await fetch(path, { ...init, headers, credentials: "include" });
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = payload?.detail ?? response.statusText;
    throw new ApiError(response.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload as T;
}

export const api = {
  async login(email: string, password: string) {
    const result = await request<{ csrf_token: string; email: string }>("/v1/session", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    setCsrf(result.csrf_token);
    return result;
  },
  async logout() {
    try {
      await request<void>("/v1/session", { method: "DELETE" });
    } finally {
      clearCsrf();
    }
  },
  profile: () =>
    request<{
      owner_id: string;
      email: string;
      timezone: string;
      prefs: Record<string, unknown>;
      prefs_version: number;
    }>("/v1/profile"),
  patchProfile: (body: Record<string, unknown>) =>
    request<Record<string, unknown>>("/v1/profile", { method: "PATCH", body: JSON.stringify(body) }),
  sources: () => request<{ items: SourceCard[] }>("/v1/sources"),
  createTextSource: (text: string, title: string) =>
    request<Created>("/v1/sources", {
      method: "POST",
      body: JSON.stringify({ kind: "text", text, title }),
    }),
  createUrlSource: (url: string, title: string, note: string) =>
    request<Created>("/v1/sources", {
      method: "POST",
      body: JSON.stringify({ kind: "url", url, title, intent_note: note }),
    }),
  uploadPdf: (file: File, title: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("title", title);
    return request<Created>("/v1/sources/upload", { method: "POST", body: form });
  },
  createEpisode: (sourceId: string, question: string, acknowledgePartial = false) =>
    request<Created>("/v1/episodes", {
      method: "POST",
      body: JSON.stringify({
        source_id: sourceId,
        question,
        acknowledge_partial: acknowledgePartial,
      }),
    }),
  episodes: () =>
    request<{
      items: Array<{
        episode_id: string;
        question: string;
        published: boolean;
        status: string | null;
        duration_ms: number | null;
      }>;
    }>("/v1/episodes"),
  episode: (id: string) => request<EpisodeDetail>(`/v1/episodes/${id}`),
  feedback: (id: string, kind: string, note: string) =>
    request<Record<string, unknown>>(`/v1/episodes/${id}/feedback`, {
      method: "POST",
      body: JSON.stringify({
        kind,
        note,
        client_event_id: crypto.randomUUID(),
      }),
    }),
  run: (id: string) => request<RunSnapshot>(`/v1/runs/${id}`),
  retryRun: (id: string) =>
    request<{ run_id: string; state: string }>(`/v1/runs/${id}/retry`, {
      method: "POST",
      body: JSON.stringify({ retry_scope: "missing" }),
    }),
  today: () => request<Today>("/v1/today"),
  feedTokens: () => request<{ items: Array<{ feed_token_id: string; created_at: string; revoked_at: string | null }> }>("/v1/feed-tokens"),
  createFeedToken: () => request<FeedToken>("/v1/feed-tokens", { method: "POST" }),
  revokeFeedToken: (id: string) =>
    request<void>(`/v1/feed-tokens/${id}`, { method: "DELETE" }),
  health: () =>
    request<{ ok: boolean; provider_mode: string; max_episode_ms: number; inline_worker: boolean }>(
      "/healthz",
    ),
  playback: (episodeId: string, sessionId: string, clientSeq: number, startMs: number, endMs: number) =>
    request<Record<string, unknown>>("/v1/playback-events", {
      method: "POST",
      body: JSON.stringify({
        episode_id: episodeId,
        session_id: sessionId,
        client_seq: clientSeq,
        kind: "progress",
        start_ms: startMs,
        end_ms: endMs,
      }),
    }),
};
