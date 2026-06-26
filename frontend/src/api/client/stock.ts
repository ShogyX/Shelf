// Library stocking domain: the operator pre-fetch pipeline (queue titles via usenet/torrent), the
// per-item ledger, and the named stocking jobs/batches.
import { req } from "./http";

export interface StockItem {
  id: number;
  stock_job_id: number | null;
  norm_key: string;
  catalog_work_id: number | null;
  work_id: number | null;
  title: string;
  author: string | null;
  media_label: string;
  media_category: string;
  popularity_norm: number;
  status: "pending" | "searching" | "downloading" | "stocked" | "unavailable" | "failed";
  size: number | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  stocked_at: string | null;
}

// Daily caps + today's usage (UTC, resets at the day boundary). 0 = unlimited.
export interface StockDailyCaps {
  searches_per_day: number;
  downloads_per_day: number;
  searches_used_today: number;
  downloads_used_today: number;
}

// A to_stock list subscription currently feeding the stock pool.
export interface StockFeedingList {
  id: number;
  provider: string;             // anilist | goodreads | openlibrary | hardcover | mal | …
  list_name: string | null;
  display_name: string;
  variant: "ebook" | "audiobook" | "both";
  to_stock: boolean;
  auto_added: number;
  last_checked_at: string | null;
}

export interface StockSummary {
  configured: boolean;          // pipeline + stock dir both set
  pipeline_configured: boolean;
  stock_dir: string | null;
  counts: Record<string, number>;
  total: number;
  daily_caps: StockDailyCaps;
  feeding_lists: StockFeedingList[];
}

export interface StockJob {
  id: number | null;            // null = the legacy "ungrouped" bucket
  name: string;
  media_category: string | null;
  dimension: string | null;
  value: string | null;
  sort: string | null;
  variant: "ebook" | "audiobook" | "both";
  requested: number;
  created_at: string | null;
  total: number;
  stocked: number;
  in_flight: number;
  pending: number;
  issues: number;               // failed + unavailable (need attention)
  progress: number;             // 0..1
  stocked_size: number;
  overall: "working" | "complete" | "needs attention" | "empty";
  counts: Record<string, number>;
}

export interface StockJobDetail extends StockJob {
  items: StockItem[];
  items_shown: number;
  problem_items: StockItem[];
}

export const stockApi = {
  // --- Library stocking (operator pre-fetch via the usenet pipeline) ---
  getStockSummary: () => req<StockSummary>("/stock/summary"),
  setStockConfig: (stock_dir: string | null) =>
    req<StockSummary>("/stock/config", { method: "PUT", body: JSON.stringify({ stock_dir }) }),
  queueStock: (body: {
    name?: string; media?: string; dimension?: string; value?: string; sort?: string;
    limit?: number; group_ids?: number[]; variant?: "ebook" | "audiobook" | "both";
    entire_catalog?: boolean; exclude_web_index?: boolean;
  }) => req<{ job_id: number | null; name: string; queued: number; skipped: number; selected: number }>(
    "/stock/queue", { method: "POST", body: JSON.stringify(body) }),
  deleteStock: (id: number) =>
    req<{ deleted: number }>(`/stock/${id}`, { method: "DELETE" }),
  // Named stocking batches (jobs).
  listStockJobs: () => req<StockJob[]>("/stock/jobs"),
  getStockJob: (id: number) => req<StockJobDetail>(`/stock/jobs/${id}`),
  retryStockJob: (id: number) =>
    req<{ requeued: number }>(`/stock/jobs/${id}/retry`, { method: "POST" }),
  deleteStockJob: (id: number, deleteFiles = false) =>
    req<{ deleted: number }>(`/stock/jobs/${id}?delete_files=${deleteFiles}`, { method: "DELETE" }),
};
