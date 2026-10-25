export type SourceKind = "URL" | "TEXT" | "PDF" | "RSS";

export interface SourceVersion {
  source_version_id: string;
  quality: "FULL" | "PARTIAL" | "NEEDS_INPUT" | "FAILED";
  state: string;
  index_state: string;
  chars: number;
  pages: number;
  extractor_version: string;
  fetched_at: string | null;
}

export interface SourceCard {
  source_id: string;
  kind: SourceKind;
  title: string;
  canonical_url: string | null;
  intent_note: string;
  created_at: string | null;
  version: SourceVersion | null;
  ready: boolean;
}

export interface Created {
  source_id?: string;
  episode_id?: string;
  run_id: string;
  state: string;
  deferred?: boolean;
  status_url: string;
  events_url: string;
}

export interface RunSnapshot {
  run_id: string;
  kind: string;
  state: string;
  phase: string | null;
  attempt_no: number;
  stop_reason: string | null;
  last_error: string | null;
  budget_usage: Record<string, number>;
  last_seq: number;
  revision: {
    revision_id: string;
    status: string;
    duration_ms: number | null;
    audio_ready: boolean;
    sha256: string | null;
  } | null;
}

export interface PlanItem {
  episode_id: string;
  position: number;
  state: "RESERVED" | "AVAILABLE" | "DEFERRED" | "DROPPED";
  reserved_ms: number;
  duration_ms: number | null;
  question: string;
  published: boolean;
  guid?: string;
  published_at?: string | null;
}

export interface Today {
  local_date: string;
  plan_id: string;
  budget_ms: number;
  reserved_ms: number;
  available_ms: number;
  total_ms: number;
  timezone: string;
  items: PlanItem[];
  promoted_runs: string[];
}

export interface Claim {
  claim_id: string;
  text: string;
  kind: string;
  critical: boolean;
  review: "SUPPORTED" | "PARTIAL" | "CONTRADICTED" | "NOT_FOUND" | string;
  evidence: Array<{
    source_version_id: string;
    chunk_id: string;
    span_start: number;
    span_end: number;
    quote: string;
    relation: string;
  }>;
}

export interface EpisodeDetail {
  episode_id: string;
  guid: string;
  question: string;
  published: boolean;
  published_at: string | null;
  duration_ms: number | null;
  revision: {
    revision_id: string;
    status: string;
    created_at: string | null;
    config: Record<string, unknown>;
    review: {
      verdicts?: Array<{ claim_id: string; verdict: string; reason: string }>;
      blocking?: string[];
    };
  } | null;
  audio: {
    duration_ms: number | null;
    sha256: string | null;
    byte_size: number | null;
    web_url: string;
    feed_path: string;
  } | null;
  script: {
    segments: Array<{ segment_id: string; heading: string; spoken_text: string; claim_ids: string[] }>;
    claims: Claim[];
    char_count: number;
    one_line_answer: string;
  } | null;
  evidence: Array<{
    chunk_id: string;
    source_title: string;
    source_kind: string;
    page_no: number | null;
    section_path: string;
    paragraph_id: string;
    version_id: string;
  }>;
  source: { source_id: string; title: string; kind: string; canonical_url: string | null } | null;
  feedback: Array<{ kind: string; note: string; created_at: string }>;
}

export interface FeedToken {
  feed_token_id: string;
  token: string;
  url: string;
}
