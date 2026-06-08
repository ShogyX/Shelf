// Operator-facing "where does this land?" picker. Sets the shared destination bookshelf
// (store.destShelfId) that every hook / acquire / add action reads and threads to the backend, so
// titles fetched from the Index or Add page drop straight onto the chosen shelf (and fire its
// automation). The choice is per-user, persisted in localStorage, and applies until changed.
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { useApp } from "../store";

export default function ShelfDestination({ className = "" }: { className?: string }) {
  const { data: shelves = [] } = useQuery({ queryKey: ["bookshelves"], queryFn: api.listBookshelves });
  const destShelfId = useApp((s) => s.destShelfId);
  const setDestShelf = useApp((s) => s.setDestShelf);

  // If the saved shelf was deleted, fall back to "library only" so we never post a stale id.
  useEffect(() => {
    if (destShelfId != null && shelves.length && !shelves.some((s) => s.id === destShelfId)) {
      setDestShelf(null);
    }
  }, [destShelfId, shelves, setDestShelf]);

  if (shelves.length === 0) return null;
  return (
    <label className={`flex items-center gap-2 text-sm text-muted ${className}`}>
      <span className="whitespace-nowrap">🗂 Save to</span>
      <select
        className="rounded-lg border border-border bg-surface px-2 py-1 text-sm text-text"
        value={destShelfId ?? ""}
        onChange={(e) => setDestShelf(e.target.value ? Number(e.target.value) : null)}
        title="Bookshelf that newly hooked / acquired titles are placed on"
      >
        <option value="">Library only</option>
        {shelves.map((s) => (
          <option key={s.id} value={s.id}>{s.name}</option>
        ))}
      </select>
    </label>
  );
}
