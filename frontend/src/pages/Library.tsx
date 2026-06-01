import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { api, ContinueItem, Work } from "../api/client";
import { useEffect, useState } from "react";
import { Badge, Button, Card, EmptyState, Spinner } from "../components/ui";
import Cover from "../components/Cover";
import SendDialog from "../components/SendDialog";
import { healthBadge } from "./Index";

function ContinueReading() {
  const { data } = useQuery({ queryKey: ["continue"], queryFn: api.continueReading });
  if (!data || data.length === 0) return null;
  return (
    <section className="mb-9">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
        Continue reading
      </h2>
      <div className="flex gap-3 overflow-x-auto pb-2 scrollbar-thin">
        {data.map((it: ContinueItem) => (
          <Link
            key={it.work_id}
            to={`/read/${it.work_id}/${it.chapter_id}`}
            className="group flex w-72 shrink-0 gap-3 rounded-xl border border-border bg-surface p-3 transition hover:border-accent/60"
          >
            <div className="h-24 w-16 shrink-0 overflow-hidden rounded-md">
              <Cover title={it.title} coverUrl={it.cover_url} small />
            </div>
            <div className="flex min-w-0 flex-1 flex-col">
              <div className="truncate font-medium leading-tight">{it.title}</div>
              <div className="mt-0.5 truncate text-xs text-muted">{it.chapter_title}</div>
              <div className="mt-auto">
                <div className="mb-1 h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
                  <div className="h-full rounded-full bg-accent" style={{ width: `${it.percent}%` }} />
                </div>
                <div className="flex items-center justify-between text-[11px] text-muted">
                  <span>{it.percent}%</span>
                  <span className="text-accent opacity-0 transition group-hover:opacity-100">Resume →</span>
                </div>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export default function Library() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [sendWork, setSendWork] = useState<Work | null>(null);
  const [query, setQuery] = useState("");
  const q = useDebounced(query.trim());
  const { data: works, isLoading } = useQuery({
    queryKey: ["works", q],
    queryFn: () => api.listWorks(q),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.deleteWork(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["works"] }),
  });

  const repair = useMutation({
    mutationFn: (id: number) => api.repairWork(id),
    onSuccess: (rep) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      const acted = rep.actions.length ? rep.actions.join("; ") : "no fixable issues found";
      alert(`Diagnosis: ${rep.health}. ${rep.detail ?? ""}\nRepair: ${acted}.`);
    },
  });

  const checkOne = useMutation({
    mutationFn: (id: number) => api.checkWorkUpdates(id),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      if (!r.checked) alert("This title's source doesn't get new chapters.");
      else if (r.error) alert(`Update check failed: ${r.error}`);
      else if (r.new_chapters > 0)
        alert(`Found ${r.new_chapters} new chapter${r.new_chapters === 1 ? "" : "s"} — gathering now.`);
      else alert(r.metadata_changed ? "Metadata refreshed; no new chapters." : "Already up to date.");
    },
  });

  const checkAll = useMutation({
    mutationFn: () => api.checkAllUpdates(),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["works"] });
      alert(
        `Checked ${r.works_checked} title${r.works_checked === 1 ? "" : "s"}: ` +
          `${r.works_updated} updated, ${r.new_chapters} new chapter${r.new_chapters === 1 ? "" : "s"}.`
      );
    },
  });

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Library</h1>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            title="Re-check ongoing titles for newly released chapters"
            disabled={checkAll.isPending}
            onClick={() => checkAll.mutate()}
          >
            {checkAll.isPending ? "Checking…" : "⟳ Check for updates"}
          </Button>
          <Link to="/add">
            <Button variant="primary">+ Add a work</Button>
          </Link>
        </div>
      </div>

      {!q && <ContinueReading />}

      <div className="relative mb-6">
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted">
          🔍
        </span>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search your library by title, author or description…"
          className="w-full rounded-xl border border-border bg-surface py-2.5 pl-10 pr-3 text-sm shadow-sm focus:border-accent focus:outline-none"
        />
        {query && (
          <button
            onClick={() => setQuery("")}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-muted hover:text-text"
          >
            clear
          </button>
        )}
      </div>

      {isLoading && <Spinner label="Loading library…" />}

      {!isLoading && (!works || works.length === 0) && (
        q ? (
          <EmptyState
            title={`No works match “${q}”`}
            hint="Try a different title, author, or keyword."
          />
        ) : (
          <EmptyState
            title="Your shelf is empty"
            hint="Add a public-domain title from Project Gutenberg or Standard Ebooks, import a file you own, or hook a permitted feed."
            action={
              <Link to="/add">
                <Button variant="primary">Add your first work</Button>
              </Link>
            }
          />
        )
      )}

      {!isLoading && q && works && works.length > 0 && (
        <p className="mb-3 text-sm text-muted">
          {works.length} result{works.length === 1 ? "" : "s"} for “{q}”
        </p>
      )}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4">
        {works?.map((w) => {
          return (
            <Card key={w.id} className="group overflow-hidden">
              <Link to={`/read/${w.id}`} className="block">
                <div className="aspect-[2/3] w-full overflow-hidden">
                  <Cover title={w.title} author={w.author} coverUrl={w.cover_url} />
                </div>
              </Link>
              <div className="space-y-1 p-3">
                <Link to={`/read/${w.id}`} className="block font-medium leading-tight hover:underline line-clamp-2">
                  {w.title}
                </Link>
                <div className="text-xs text-muted line-clamp-1">{w.author ?? "Unknown author"}</div>
                <div className="flex flex-wrap items-center gap-1.5 pt-1">
                  {w.hooked && <Badge tone="violet">hooked</Badge>}
                  <Badge tone={w.status === "complete" ? "green" : "amber"}>{w.status}</Badge>
                  {(() => {
                    const total = w.total_chapters_expected ?? w.total_chapters_known;
                    return <Badge>{w.chapters_fetched}{total ? `/${total}` : ""} ch</Badge>;
                  })()}
                  {(() => {
                    const hb = healthBadge(w.health);
                    return hb ? (
                      <span title={w.health_detail ?? undefined}>
                        <Badge tone={hb.tone}>{hb.label}</Badge>
                      </span>
                    ) : null;
                  })()}
                  {(() => {
                    if (!w.last_update_at) return null;
                    const days = (Date.now() - new Date(w.last_update_at).getTime()) / 86400000;
                    return days <= 14 ? (
                      <span title={`New content found ${new Date(w.last_update_at).toLocaleString()}`}>
                        <Badge tone="green">updated</Badge>
                      </span>
                    ) : null;
                  })()}
                </div>
                {(() => {
                  const total = w.total_chapters_expected ?? w.total_chapters_known;
                  if (!w.hooked || !total || w.chapters_fetched >= total) return null;
                  const pct = Math.min(100, Math.round((w.chapters_fetched / total) * 100));
                  return (
                    <div className="pt-1">
                      <div className="h-1 w-full overflow-hidden rounded-full bg-surface-2">
                        <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
                      </div>
                      <div className="mt-0.5 text-[10px] text-muted">
                        gathering {w.chapters_fetched}/{total}
                      </div>
                    </div>
                  );
                })()}
                <div className="flex flex-wrap gap-1.5 pt-2 opacity-100 transition sm:opacity-0 sm:group-hover:opacity-100">
                  <Button size="sm" variant="primary" onClick={() => navigate(`/read/${w.id}`)}>
                    Read
                  </Button>
                  <Button size="sm" variant="outline" title="Send to Kindle / export EPUB"
                    onClick={() => setSendWork(w)}>
                    📤 Send
                  </Button>
                  {(w.health === "incomplete" || w.health === "no_chapters") && (
                    <Button
                      size="sm"
                      variant="outline"
                      title={w.health_detail ?? "Diagnose and fix missing chapters"}
                      disabled={repair.isPending && repair.variables === w.id}
                      onClick={() => repair.mutate(w.id)}
                    >
                      {repair.isPending && repair.variables === w.id ? "Fixing…" : "🩺 Fix"}
                    </Button>
                  )}
                  {w.hooked && w.status === "ongoing" && (
                    <Button
                      size="sm"
                      variant="outline"
                      title={
                        w.last_checked_at
                          ? `Check for new chapters (last checked ${new Date(w.last_checked_at).toLocaleString()})`
                          : "Check for new chapters"
                      }
                      disabled={checkOne.isPending && checkOne.variables === w.id}
                      onClick={() => checkOne.mutate(w.id)}
                    >
                      {checkOne.isPending && checkOne.variables === w.id ? "Checking…" : "⟳ Updates"}
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => {
                      if (confirm(`Remove "${w.title}" from your library?`)) del.mutate(w.id);
                    }}
                  >
                    Remove
                  </Button>
                </div>
              </div>
            </Card>
          );
        })}
      </div>

      {sendWork && (
        <SendDialog workId={sendWork.id} title={sendWork.title} onClose={() => setSendWork(null)} />
      )}
    </main>
  );
}
