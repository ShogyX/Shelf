// Netflix-style discovery rows for the Index page: a "Most Popular" lane plus the marquee genre
// and theme lanes, per media section. Each lane is a horizontal scroller of compact poster cards
// (click → the shared detail modal to hook), with a "Browse →" link to the full sorted grid.
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, CatalogGroup, CatalogRow } from "../../api/client";
import { Badge, Spinner } from "../ui";
import { mediaTone } from "./CatalogCard";

const BUCKET_LABEL: Record<string, string> = { comic: "Comics", text: "Novels & Books" };

function browseHref(row: CatalogRow): string {
  const dim = row.kind === "popular" ? "popular" : row.kind;
  const val = row.slug || "all";
  return `/browse/${dim}/${encodeURIComponent(val)}?media=${row.media_bucket}`;
}

export function CatalogRows({ onOpenDetail }: { onOpenDetail: (g: CatalogGroup) => void }) {
  const rows = useQuery({ queryKey: ["catalog-rows"], queryFn: () => api.catalogRows() });

  if (rows.isLoading) return <div className="mt-4"><Spinner label="Loading discovery…" /></div>;
  const data = rows.data ?? [];
  if (data.length === 0) {
    return (
      <p className="mt-3 text-sm text-muted">
        No works discovered yet — index a fiction site and they'll appear here, sorted by
        popularity and genre, as the crawler finds and enriches them.
      </p>
    );
  }

  // Section the lanes by media bucket (Comics / Novels), preserving server order within each.
  const buckets: string[] = [];
  for (const r of data) if (!buckets.includes(r.media_bucket)) buckets.push(r.media_bucket);

  return (
    <div className="mt-4 space-y-8">
      {buckets.map((bucket) => (
        <section key={bucket}>
          <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted">
            {BUCKET_LABEL[bucket] ?? bucket}
          </h3>
          <div className="space-y-5">
            {data.filter((r) => r.media_bucket === bucket).map((row) => (
              <Lane key={`${row.kind}:${row.slug}`} row={row} onOpenDetail={onOpenDetail} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function Lane({ row, onOpenDetail }: { row: CatalogRow; onOpenDetail: (g: CatalogGroup) => void }) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-3">
        <h4 className="text-base font-semibold text-text">
          {row.label}
          {row.kind !== "popular" && (
            <span className="ml-1.5 text-xs font-normal text-muted">
              {row.count.toLocaleString()}
            </span>
          )}
        </h4>
        <Link to={browseHref(row)} className="shrink-0 text-xs text-accent hover:underline">
          Browse →
        </Link>
      </div>
      <div className="flex gap-3 overflow-x-auto pb-2 [scrollbar-width:thin]">
        {row.items.map((g) => (
          <PosterCard key={g.id || g.norm_key} group={g} onOpen={() => onOpenDetail(g)} />
        ))}
      </div>
    </div>
  );
}

function PosterCard({ group, onOpen }: { group: CatalogGroup; onOpen: () => void }) {
  return (
    <button
      onClick={onOpen}
      className="group w-32 shrink-0 text-left"
      title={`${group.title} — view details & add`}
    >
      <div className="relative aspect-[2/3] w-full overflow-hidden rounded-lg border border-border bg-surface">
        {group.cover_url ? (
          <img
            src={group.cover_url}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover transition group-hover:scale-105"
            onError={(e) => (e.currentTarget.style.visibility = "hidden")}
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center p-2 text-center text-xs text-muted">
            {group.title}
          </div>
        )}
        {group.hooked_work_id && (
          <span className="absolute left-1 top-1">
            <Badge tone="green">in library</Badge>
          </span>
        )}
      </div>
      <div className="mt-1 line-clamp-2 text-xs font-medium leading-tight text-text group-hover:text-accent">
        {group.title}
      </div>
      <div className="mt-0.5 flex items-center gap-1 text-[10px] text-muted">
        <Badge tone={mediaTone(group.media_label)}>{group.media_label}</Badge>
        {group.chapters != null && <span>{group.chapters.toLocaleString()} ch</span>}
      </div>
    </button>
  );
}
