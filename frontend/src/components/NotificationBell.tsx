import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { qk } from "../api/queryKeys";

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
        title="Notifications"
        className="relative rounded-lg border border-border px-2.5 py-1.5 text-sm hover:bg-surface-2"
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
          <div className="absolute right-0 z-50 mt-2 w-80 max-w-[90vw] rounded-xl border border-border bg-surface shadow-2xl">
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
