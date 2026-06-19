// Sources domain: reading sources and their adapters, and the background job queue.
import { req } from "./http";

export interface Source {
  id: number;
  key: string;
  display_name: string;
  base_url: string | null;
  adapter_key: string;
  license_basis: string;
  tos_permitted: boolean;
  robots_respected: boolean;
  render_js: boolean;
  min_request_interval_s: number;
  max_daily_requests: number;
  has_auth: boolean;       // a credential (e.g. J-Novel token) is stored
  supports_auth: boolean;  // this source accepts an access token
  auth_token?: string;     // write-only: set to store, "" to clear (never returned)
}

export interface AdapterInfo {
  key: string;
  display_name: string;
  license_basis: string;
  tos_permitted_default: boolean;
  needs_attestation: boolean;
  description: string;
  enabled: boolean;
}

export interface Job {
  id: number;
  work_id: number;
  kind: string;
  status: string;
  attempts: number;
  last_error: string | null;
  cursor: Record<string, unknown> | null;
  scheduled_for: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export const sourcesApi = {
  listSources: () => req<Source[]>("/sources"),
  listAdapters: () => req<AdapterInfo[]>("/adapters"),
  updateSource: (id: number, patch: Partial<Source>) =>
    req<Source>(`/sources/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),

  listJobs: () => req<Job[]>("/jobs"),
  retryJob: (id: number) => req<Job>(`/jobs/${id}/retry`, { method: "POST" }),
  deleteJob: (id: number) => req<{ deleted: number }>(`/jobs/${id}`, { method: "DELETE" }),
  pauseJob: (id: number) => req<Job>(`/jobs/${id}/pause`, { method: "POST" }),
  resumeJob: (id: number) => req<Job>(`/jobs/${id}/resume`, { method: "POST" }),
};
