// Wanted domain: the requests + tracking dashboard (replaces the deprecated Watchlist). A regular
// user sees only THEIR requested titles + the series/authors THEY track; an admin can additionally
// view the whole instance (scope=global) with a per-user breakdown, or drill into one user. Tracking
// follow/unfollow/pause still go through the subscriptions API; this client only READS tracking
// (which carries the follow state + admin username). All dates are ISO strings.
import { req } from "./http";

// A request's live acquisition state (the coloured status chip on the page).
export type WantedState =
  | "requested" | "searching" | "downloading" | "available" | "unavailable" | "upcoming";

export interface WantedStateCounts {
  requested: number;
  searching: number;
  downloading: number;
  available: number;
  unavailable: number;
  upcoming: number;
  total: number;
}

export interface WantedTrackingCounts {
  total: number;
  active: number;
  paused: number;
  auto_added_total: number;
}

export interface WantedUserBreakdown {
  user_id: number | null;
  username: string;
  requests: WantedStateCounts;
  tracking: WantedTrackingCounts;
}

export interface WantedOverview {
  scope: "me" | "global";
  is_admin: boolean;
  requests: WantedStateCounts;
  tracking: WantedTrackingCounts;
  // Present only for an admin viewing scope=global.
  per_user: WantedUserBreakdown[] | null;
}

// One requested title with its acquisition state. Requester fields are admin-only; requested_at is
// only the caller's own request time in the per-user (me/user_id) view.
export interface WantedRequest {
  id: number;
  title: string;
  author: string | null;
  variant: "ebook" | "audiobook";
  formats: ("ebook" | "audiobook")[]; // every format this work was requested in (read/listen badges)
  language: string | null;            // ISO code of the matched edition (grabbed/wanted)
  state: WantedState;
  status: string;                     // raw ContentRequest.status
  cover_url: string | null;
  catalog_work_id: number | null;
  work_id: number | null;             // the imported Work (set when available) → open it in the library
  audio_work_id: number | null;       // shared audio Work for a resolved audiobook request → play it
  series: string | null;
  series_position: number | null;
  origin: "request" | "series" | "goodreads";
  origin_detail: string | null;
  failure_reason: string | null;
  first_requested_at: string | null;
  requested_at: string | null;        // when the CALLER requested it (per-user view only)
  last_attempt_at: string | null;
  resolved_at: string | null;
  release_date: string | null;        // upcoming title's expected release date
  download_status: string | null;     // live DownloadJob status while acquiring
  download_mb_left: number | null;
  requester_count: number | null;     // admin only
  requesters: string[] | null;        // admin only ("system" for an unattributed request)
}

export interface WantedRequestsPage {
  items: WantedRequest[];
  total: number;
  limit: number;
  offset: number;
}

// A series/author the user tracks (Subscription), with its follow state. username is admin-only.
export interface Tracked {
  id: number;
  kind: "author" | "series";
  display_name: string;
  active: boolean;
  auto_request: boolean;
  auto_added: number;
  last_checked_at: string | null;
  created_at: string | null;
  state: "up_to_date" | "gathering" | "paused";
  user_id: number | null;
  username: string | null;
}

// Mass-rescan queue progress for the admin progress strip.
export interface WantedRescanStatus {
  total: number;   // the active run's size (0 when idle)
  done: number;    // max(0, total - queued)
  queued: number;  // rows still holding rescan_queued_at
  active: boolean; // queued > 0
}

// A recently-added Work for the admin dashboard "Recently added" rails.
export interface WantedRecentWork {
  work_id: number;
  title: string;
  author: string | null;
  cover_url: string | null;
  language: string | null;
  media_kind: string;
  added_at: string | null;
}

// One imported/tracked external reading list (Goodreads / AniList / …) for the dashboard rail.
export interface WantedTrackedList {
  id: number;
  provider: string;
  list_ref: string;
  list_name: string | null;
  display_name: string;
  variant: string;
  active: boolean;
  total: number;
  done: number;
  pending: number;
  last_checked_at: string | null;
  last_error: string | null;
  created_at: string | null;
}

// Overseerr-style admin dashboard: whole-instance rails.
export interface WantedDashboard {
  recent_requests: WantedRequest[];
  recent_ebooks: WantedRecentWork[];
  recent_audiobooks: WantedRecentWork[];
  tracked_lists: WantedTrackedList[];
  tracking: Tracked[];
  user_requests: WantedUserBreakdown[];
  upcoming: WantedRequest[];
}

export type WantedScope = "me" | "global";

export interface WantedRequestsParams {
  scope?: WantedScope;
  state?: WantedState;
  user_id?: number;
  sort?: "newest" | "title" | "author";
  limit?: number;
  offset?: number;
}

export const wantedApi = {
  wantedOverview: (scope: WantedScope = "me") =>
    req<WantedOverview>(`/wanted/overview?scope=${scope}`),

  listWantedRequests: (params: WantedRequestsParams = {}) => {
    const p = new URLSearchParams();
    p.set("scope", params.scope ?? "me");
    if (params.state) p.set("state", params.state);
    if (params.user_id != null) p.set("user_id", String(params.user_id));
    if (params.sort) p.set("sort", params.sort);
    if (params.limit != null) p.set("limit", String(params.limit));
    if (params.offset != null) p.set("offset", String(params.offset));
    return req<WantedRequestsPage>(`/wanted/requests?${p.toString()}`);
  },

  listWantedTracking: (scope: WantedScope = "me", userId?: number) => {
    const p = new URLSearchParams();
    p.set("scope", scope);
    if (userId != null) p.set("user_id", String(userId));
    return req<Tracked[]>(`/wanted/tracking?${p.toString()}`);
  },

  // Admin: force an immediate re-acquire of one requested title.
  recheckWanted: (id: number) =>
    req<WantedRequest>(`/wanted/requests/${id}/recheck`, { method: "POST" }),

  // Admin: mass re-acquire every searchable request in scope — exactly one of these.
  rescanWanted: (
    body: { all: true } | { author: string } | { series: string } | { ids: number[] },
  ) => req<{ queued: number }>("/wanted/rescan", { method: "POST", body: JSON.stringify(body) }),

  getWantedRescanStatus: () => req<WantedRescanStatus>("/wanted/rescan/status"),

  // Overseerr-style dashboard rails. scope="me" (default) = the caller's own; "global" = admin whole-instance.
  getWantedDashboard: (scope: WantedScope = "me") =>
    req<WantedDashboard>(`/wanted/dashboard?scope=${scope}`),
};
