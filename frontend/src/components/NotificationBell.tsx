import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { qk } from "../api/queryKeys";
import { useEscapeClose } from "./ui";

function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const DOT: Record<string, string> = {
  info: "bg-accent", warn: "bg-amber-500", error: "bg-red-500",
};

export function NotificationBell() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  useEscapeClose(open, () => setOpen(false));

  // The badge polls a cheap count; the list is fetched only while the dropdown is open.
  const unread = useQuery({
    queryKey: qk.notifUnread(),
    queryFn: api.getUnreadCount,
    refetchInterval: 30_000,
  });
  const list = useQuery({
    queryKey: qk.notifications(),
    queryFn: () => api.listNotifications({ limit: 30 }),
    enabled: open,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: qk.notifUnread() });
    qc.invalidateQueries({ queryKey: qk.notifications() });
  };
  const readOne = useMutation({ mutationFn: api.markNotificationRead, onSuccess: refresh });
  const readAll = useMutation({ mutationFn: api.markAllNotificationsRead, onSuccess: refresh });

  const count = unread.data?.count ?? 0;
  const items = list.data ?? [];

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="Notifications" aria-label="Notifications" aria-haspopup="menu" aria-expanded={open}
        className="relative flex h-[38px] w-[38px] items-center justify-center rounded-[11px] border border-[var(--hair,var(--border))] bg-surface text-text transition hover:bg-surface-2"
      >
        <span>🔔</span>
        {count > 0 && (
          <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-semibold text-white">
            {count > 99 ? "99+" : count}
          </span>
        )}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          {/* The bell is the leftmost of the top-right trio, so a trigger-anchored 320px panel ran off
              the LEFT edge on a phone. On mobile anchor it to the viewport (full-width below the bar);
              on desktop keep it dropping from the bell. */}
          <div className="sp-pop fixed inset-x-2 top-[calc(env(safe-area-inset-top)_+_3.75rem)] z-50 overflow-hidden rounded-[15px] border border-[var(--hair-strong,var(--border))] bg-surface shadow-[var(--pop-shadow)] sm:absolute sm:inset-x-auto sm:right-0 sm:top-full sm:mt-2 sm:w-80">
            <div className="flex items-center justify-between border-b border-border px-3 py-2">
              <span className="text-sm font-semibold">Notifications</span>
              {count > 0 && (
                <button
                  className="text-xs text-muted hover:text-text"
                  onClick={() => readAll.mutate()}
                >
                  Mark all read
                </button>
              )}
            </div>
            <div className="max-h-96 overflow-y-auto">
              {items.length === 0 ? (
                <div className="px-3 py-6 text-center text-sm text-muted">
                  {list.isLoading ? "Loading…" : "You're all caught up."}
                </div>
              ) : (
                items.map((n) => (
                  <button
                    key={n.id}
                    onClick={() => n.read_at === null && readOne.mutate(n.id)}
                    className={`flex w-full items-start gap-2 border-b border-border px-3 py-2.5 text-left last:border-0 hover:bg-surface-2 ${
                      n.read_at === null ? "" : "opacity-60"
                    }`}
                  >
                    <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${DOT[n.level] ?? "bg-accent"}`} />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-text">{n.title}</span>
                      {n.body && <span className="block truncate text-xs text-muted">{n.body}</span>}
                      <span className="block text-[11px] text-muted">{timeAgo(n.created_at)}</span>
                    </span>
                  </button>
                ))
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
