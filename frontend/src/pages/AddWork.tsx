import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, AdapterInfo, CrawlPolicy, WatchedFolder } from "../api/client";
import { Badge, Button, Card, InfoHint, Spinner, Tabs, Toggle } from "../components/ui";
import { CrawlPolicyFields } from "../components/CrawlPolicy";
import { useConfirm } from "../components/confirm";
import { useShelfPrompt } from "../components/ShelfPrompt";
import { useHasPermission, useIsAdmin } from "../auth";
import { useApp } from "../store";
import { SourcesTab } from "./Sources";

const REF_HINTS: Record<string, string> = {
  gutenberg: "Gutenberg book ID, e.g. 1342 (Pride and Prejudice)",
  standardebooks: "Ebook URL or author/title slug, e.g. jane-austen/pride-and-prejudice",
  generic_feed: "RSS/Atom/OPDS feed URL or a chapter-index page URL",
  jnovel: "J-Novel series URL or slug, e.g. https://j-novel.club/series/<slug>",
  comix: "comix.to series URL, e.g. https://comix.to/title/<hid>-<slug>",
  memory: "Any ref (demo) — generates a local test serial",
};

// Sources that aren't "hook a reference" — they get their own UI. local_import and local_folder are
// demoted to their own tabs (Import files / Watched folders), so they're filtered out of the grid.
const HIDDEN_ADAPTERS = new Set(["web_index", "local_import", "local_folder"]);

type TabId = "add" | "import" | "folders" | "sources";

export default function AddPage() {
  const canAdd = useHasPermission("add.use");
  const canSources = useHasPermission("sources.view");
  const [params, setParams] = useSearchParams();

  const tabs: { id: TabId; label: string }[] = [
    ...(canAdd
      ? ([
          { id: "add", label: "Add a title" },
          { id: "import", label: "Import files" },
          { id: "folders", label: "Watched folders" },
        ] as const)
      : []),
    ...(canSources ? ([{ id: "sources", label: "Sources" }] as const) : []),
  ];

  const fallback: TabId = canAdd ? "add" : "sources";
  const urlTab = params.get("tab") as TabId | null;
  const active: TabId = tabs.some((t) => t.id === urlTab) ? (urlTab as TabId) : fallback;

  const setActive = (id: string) => {
    const next = new URLSearchParams(params);
    next.set("tab", id);
    setParams(next, { replace: true });
  };

  return (
    <main className="mx-auto max-w-2xl px-4 py-8">
      <h1 className="mb-4 text-2xl font-semibold">Add</h1>
      <Tabs tabs={tabs} active={active} onChange={setActive} className="mb-6" />
      {active === "add" && <AddTitleTab />}
      {active === "import" && <ImportFilesTab />}
      {active === "folders" && <LocalFolders />}
      {active === "sources" && <SourcesTab />}
    </main>
  );
}

function AddTitleTab() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  const pickShelf = useShelfPrompt();
  const adapters = useQuery({ queryKey: ["adapters"], queryFn: api.listAdapters });
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.listSources });

  const [selected, setSelected] = useState<string>("gutenberg");
  const [ref, setRef] = useState("");
  const [attest, setAttest] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPolicy, setShowPolicy] = useState(false);
  const [policy, setPolicy] = useState<Partial<CrawlPolicy>>({});
  const [updateIndexed, setUpdateIndexed] = useState(false);

  // Default the selection to the first VISIBLE adapter once they load — the hard-coded "gutenberg"
  // default highlights nothing (and submits a rejected hook) when gutenberg is disabled/hidden. The
  // demoted local_* adapters are filtered out, so the default can never land on them.
  useEffect(() => {
    const visible = adapters.data?.filter((a) => a.enabled && !HIDDEN_ADAPTERS.has(a.key)) ?? [];
    if (visible.length && !visible.some((a) => a.key === selected)) setSelected(visible[0].key);
  }, [adapters.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const adapter: AdapterInfo | undefined = adapters.data?.find((a) => a.key === selected);
  const source = sources.data?.find((s) => s.key === selected);
  const blocked = source && !source.tos_permitted;
  const trimmed = ref.trim();
  const isUrl = /^https?:\/\//i.test(trimmed);

  async function hook() {
    setError(null);
    setBusy(true);
    try {
      const shelfId = await pickShelf();
      if (shelfId === undefined) return; // cancelled → abort
      const work = await api.hook(selected, trimmed, policy, shelfId ?? undefined);
      await qc.invalidateQueries({ queryKey: ["works"] });
      navigate(`/read/${work.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const indexSite = useMutation({
    // Index ignores attestation/policy — it crawls a whole site, not a single permitted title.
    mutationFn: () => api.addIndexSite({ url: trimmed, update_indexed: updateIndexed }),
    onSuccess: () => {
      setRef("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["index-sites"] });
      useApp.getState().toast("Indexing started — watch progress on Jobs", "success");
    },
    onError: (e) => setError((e as Error).message),
  });

  return (
    <div>
      <p className="mb-6 text-sm text-muted">
        Shelf only ingests sources you are permitted to read. Choose a source, then hook a title.
      </p>

      {adapters.isLoading && <Spinner label="Loading sources…" />}

      <div className="mb-5 grid gap-2 sm:grid-cols-2">
        {adapters.data
          ?.filter((a) => a.enabled && !HIDDEN_ADAPTERS.has(a.key))
          .map((a) => (
            <button
              key={a.key}
              onClick={() => {
                setSelected(a.key);
                setError(null);
              }}
              className={`rounded-xl border p-3 text-left transition ${
                selected === a.key ? "border-accent bg-surface-2" : "border-border hover:bg-surface-2"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium">{a.display_name}</span>
                {a.needs_attestation && <Badge tone="amber">attest</Badge>}
              </div>
              <p className="mt-1 text-xs text-muted line-clamp-2">{a.description}</p>
              <div className="mt-2">
                <Badge tone="violet">{a.license_basis}</Badge>
              </div>
            </button>
          ))}
      </div>

      <Card className="p-4">
        <div className="space-y-3">
          <label className="block text-sm font-medium">Work reference or site URL</label>
          <input
            value={ref}
            onChange={(e) => setRef(e.target.value)}
            placeholder={REF_HINTS[selected] ?? "Source reference"}
            className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm"
          />
          <p className="text-xs text-muted">{REF_HINTS[selected]}</p>

          {adapter?.needs_attestation && (
            <label className="flex items-start gap-2 rounded-lg border border-amber-400/30 bg-amber-500/10 p-3 text-sm">
              <input
                type="checkbox"
                checked={attest}
                onChange={(e) => setAttest(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                I attest that I am permitted to ingest this source (its ToS or the author's
                license allows personal copying). Shelf will still obey robots.txt and rate
                limits.
              </span>
            </label>
          )}

          {blocked && (
            <div className="rounded-lg border border-red-400/30 bg-red-500/10 p-3 text-sm">
              This source is not enabled. Enable it (and confirm you're permitted) on the{" "}
              <button className="underline" onClick={() => navigate("/add?tab=sources")}>
                Sources
              </button>{" "}
              tab first.
            </div>
          )}

          <div className="rounded-lg border border-border p-3">
            <button
              type="button"
              className="text-xs text-muted underline"
              onClick={() => setShowPolicy((s) => !s)}
            >
              {showPolicy ? "Hide" : "Crawl speed & schedule (optional)"}
            </button>
            {showPolicy && (
              <div className="mt-3">
                <p className="mb-2 text-xs text-muted">
                  Throttle how fast / how much this title's background crawl runs, and
                  restrict it to certain hours. Leave blank to use the source defaults.
                  (Editable later in the Jobs tab.)
                </p>
                <CrawlPolicyFields value={policy} onChange={setPolicy} />
              </div>
            )}
          </div>
        </div>

        {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

        <div className="mt-4 flex items-center gap-2">
          <Button
            variant="primary"
            disabled={
              busy ||
              !trimmed ||
              (adapter?.needs_attestation && !attest) ||
              !!blocked
            }
            onClick={hook}
          >
            {busy ? "Working…" : "Hook & backfill"}
          </Button>
          {isAdmin && (
            <Button
              variant="outline"
              disabled={!isUrl || indexSite.isPending}
              title={isUrl ? undefined : "Index needs a full site URL"}
              onClick={() => indexSite.mutate()}
            >
              {indexSite.isPending ? "Starting…" : "Index"}
            </Button>
          )}
          <InfoHint
            align="right"
            className="ml-auto"
            text={
              <>
                <strong>Hook</strong> adds a single title from the selected source and backfills its
                chapters into your library.
                <br />
                <br />
                <strong>Index</strong> (admin) crawls a whole site to discover every title — the
                results appear on the Catalog page. Needs a full site URL.
              </>
            }
          />
        </div>

        {isAdmin && isUrl && (
          <label className="mt-3 flex items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={updateIndexed}
              onChange={(e) => setUpdateIndexed(e.target.checked)}
            />
            Update already-indexed content (re-fetch pages crawled before). Off by default:
            re-adding a source resumes without repeating what was already indexed.
          </label>
        )}
      </Card>
    </div>
  );
}

function ImportFilesTab() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const pickShelf = useShelfPrompt();
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setError(null);
    setBusy(true);
    try {
      if (!file) throw new Error("Choose a file to import.");
      const shelfId = await pickShelf();
      if (shelfId === undefined) return; // cancelled → abort
      const work = await api.importFile(file, shelfId ?? undefined);
      await qc.invalidateQueries({ queryKey: ["works"] });
      navigate(`/read/${work.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-4">
      <div className="space-y-3">
        <label className="block text-sm font-medium">
          Upload EPUB / TXT / Markdown / PDF / CBZ / CBR
        </label>
        <input
          type="file"
          accept=".epub,.txt,.md,.markdown,.text,.pdf,.cbz,.cbr"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="block w-full text-sm text-muted file:mr-3 file:rounded-lg file:border file:border-border file:bg-surface-2 file:px-3 file:py-2 file:text-text"
        />
        <p className="text-xs text-muted">Only import files you legally own.</p>
      </div>
      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
      <div className="mt-4 flex justify-end">
        <Button variant="primary" disabled={busy || !file} onClick={submit}>
          {busy ? "Working…" : "Import"}
        </Button>
      </div>
    </Card>
  );
}

function LocalFolders() {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [path, setPath] = useState("");
  const [recursive, setRecursive] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const folders = useQuery({ queryKey: ["folders"], queryFn: api.listFolders });

  const add = useMutation({
    mutationFn: () => api.addFolder(path.trim(), recursive),
    onSuccess: () => {
      setPath("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["folders"] });
      qc.invalidateQueries({ queryKey: ["works"] });
    },
    onError: (e) => setError((e as Error).message),
  });
  const rescan = useMutation({
    mutationFn: (id: number) => api.rescanFolder(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["folders"] });
      qc.invalidateQueries({ queryKey: ["works"] });
    },
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteFolder(id, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["folders"] });
      qc.invalidateQueries({ queryKey: ["works"] });
    },
  });

  return (
    <Card className="p-4">
      <label className="block text-sm font-medium">Map a local folder</label>
      <p className="mb-3 mt-1 text-xs text-muted">
        Shelf imports every EPUB / TXT / Markdown / PDF / CBZ / CBR file in this directory and
        watches it — new and changed files appear in your library automatically.
      </p>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && path.trim() && add.mutate()}
          placeholder="/data/books"
          className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm"
        />
        <Button variant="primary" disabled={!path.trim() || add.isPending} onClick={() => add.mutate()}>
          {add.isPending ? "Scanning…" : "Map & watch"}
        </Button>
      </div>
      <div className="mt-2">
        <Toggle checked={recursive} onChange={setRecursive} label="Include subfolders" />
      </div>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}

      {(folders.data?.length ?? 0) > 0 && (
        <ul className="mt-4 divide-y divide-border rounded-lg border border-border">
          {folders.data!.map((f: WatchedFolder) => (
            <li key={f.id} className="flex items-center justify-between gap-2 px-3 py-2">
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{f.path}</div>
                <div className="text-xs text-muted">
                  {f.works} works · {f.file_count} files
                  {f.recursive ? " · recursive" : ""}
                  {f.last_error ? ` · ⚠ ${f.last_error}` : ""}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button size="sm" variant="ghost" onClick={() => rescan.mutate(f.id)}>
                  Rescan
                </Button>
                <Button size="sm" variant="danger" onClick={async () => {
                  if (await confirm({
                    title: "Unmap folder",
                    message: `Stop watching “${f.path}”? Imported works from this folder are removed from your library (the files on disk are untouched).`,
                    danger: true,
                  })) remove.mutate(f.id);
                }}>
                  ✕
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
