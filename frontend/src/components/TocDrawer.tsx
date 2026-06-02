import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import RelatedTitles from "./RelatedTitles";
import { Spinner } from "./ui";

export default function TocDrawer({
  workId,
  currentChapterId,
  onClose,
  onPick,
}: {
  workId: number;
  currentChapterId?: number;
  onClose: () => void;
  onPick: (chapterId: number) => void;
}) {
  const chapters = useQuery({
    queryKey: ["chapters", workId],
    queryFn: () => api.listChapters(workId),
  });

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/30" onClick={onClose} />
      <aside className="fixed left-0 top-0 z-50 flex h-full w-80 max-w-[85vw] flex-col border-r border-border bg-surface shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h3 className="font-semibold">Contents</h3>
          <button onClick={onClose} className="text-muted hover:text-text">
            ✕
          </button>
        </div>
        <div className="scrollbar-thin flex-1 overflow-y-auto">
          <RelatedTitles workId={workId} />
          <div className="p-2">
          {chapters.isLoading && <Spinner label="Loading…" />}
          {chapters.data?.items.map((c) => {
            const active = c.id === currentChapterId;
            return (
              <button
                key={c.id}
                disabled={!c.has_content}
                onClick={() => onPick(c.id)}
                className={`flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm transition ${
                  active ? "bg-accent text-accent-fg" : "hover:bg-surface-2"
                } ${!c.has_content ? "opacity-40" : ""}`}
              >
                <span className="truncate">
                  <span className="mr-2 text-xs opacity-60">{c.index}</span>
                  {c.title}
                </span>
                {!c.has_content && <span className="text-xs">⏳</span>}
              </button>
            );
          })}
          </div>
        </div>
      </aside>
    </>
  );
}
