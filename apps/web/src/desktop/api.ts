export interface Entry {
  id: string; title: string; instructions: string; status: string; stage: string;
  target_seconds: number; created_at: string; updated_at: string; error: string;
  script: string; summary: string; model: string; duration_seconds: number;
  segment_done: number; segment_total: number; source_count: number; audio_path: string;
}
export interface Source {
  id: string; name: string; kind: string; location: string; status: string;
  text: string; error: string; chars: number; external: number;
}
export interface Detail extends Entry {
  sources: Source[];
  events: { seq: number; created_at: string; message: string }[];
}
export interface Status {
  tts: { state: string; error: string; model: string };
  cpa: { connected: boolean; models: string[]; error: string };
  model: string; cpa_url: string; active_id: string | null; data_dir: string; formats: string[];
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/local/api${path}`, {
    ...init, headers: { "X-Local-App": "1", ...init.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: `请求失败：${response.status}` }));
    throw new Error(typeof body.detail === "string" ? body.detail : "请求内容不符合要求。");
  }
  return response.json() as Promise<T>;
}
export const audioUrl = (id: string) => `/local/api/entries/${id}/audio`;
