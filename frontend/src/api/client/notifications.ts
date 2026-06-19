// Notifications domain: notification channels, per-event preferences (user + admin), the in-app
// feed and unread count, admin global channel + broadcast, and each user's own Goodreads connection.
import { req } from "./http";

export interface NotificationChannel {
  id: number;
  kind: string;                       // ntfy | pushover | telegram | discord | slack | email | apprise
  label: string | null;
  config: Record<string, unknown>;    // redacted (secret fields → '<field>_set' booleans)
  enabled: boolean;
}

export interface NotificationEvent {
  key: string;
  label: string;
  description: string;
  audience: string;                   // user | admin
  category: string;
  default_on: boolean;
  enabled: boolean;                   // effective for this viewer
}

export interface NotificationItem {
  id: number;
  event_key: string;
  title: string;
  body: string;
  level: string;                      // info | warn | error
  created_at: string;
  read_at: string | null;
}

export interface GoodreadsConnection {
  connected: boolean;
  id?: number | null;
  enabled?: boolean;
  goodreads_user_id?: string | null;
  shelf?: string | null;
  last_sync_at?: string | null;
  last_error?: string | null;
}

export const notificationsApi = {
  // --- Notifications: channels, per-event preferences, the in-app feed, admin broadcast ---
  listChannels: () => req<NotificationChannel[]>("/notifications/channels"),
  createChannel: (body: { kind: string; label?: string; config: Record<string, unknown>; enabled?: boolean }) =>
    req<NotificationChannel>("/notifications/channels", { method: "POST", body: JSON.stringify(body) }),
  updateChannel: (id: number, body: { label?: string; config?: Record<string, unknown>; enabled?: boolean }) =>
    req<NotificationChannel>(`/notifications/channels/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteChannel: (id: number) =>
    req<{ deleted: boolean }>(`/notifications/channels/${id}`, { method: "DELETE" }),
  testChannel: (id: number) =>
    req<{ ok: boolean; error: string | null }>(`/notifications/channels/${id}/test`, { method: "POST" }),
  getNotifPrefs: () => req<NotificationEvent[]>("/notifications/prefs"),
  setNotifPrefs: (selected: Record<string, boolean>) =>
    req<NotificationEvent[]>("/notifications/prefs", { method: "PUT", body: JSON.stringify({ selected }) }),
  getAdminNotifPrefs: () => req<NotificationEvent[]>("/notifications/admin/prefs"),
  setAdminNotifPrefs: (selected: Record<string, boolean>) =>
    req<NotificationEvent[]>("/notifications/admin/prefs", { method: "PUT", body: JSON.stringify({ selected }) }),
  listNotifications: (opts?: { unreadOnly?: boolean; limit?: number }) => {
    const q = new URLSearchParams();
    if (opts?.unreadOnly) q.set("unread_only", "true");
    if (opts?.limit) q.set("limit", String(opts.limit));
    return req<NotificationItem[]>(`/notifications${q.toString() ? `?${q}` : ""}`);
  },
  getUnreadCount: () => req<{ count: number }>("/notifications/unread-count"),
  markNotificationRead: (id: number) =>
    req<{ ok: boolean }>(`/notifications/${id}/read`, { method: "POST" }),
  markAllNotificationsRead: () => req<{ count: number }>("/notifications/read-all", { method: "POST" }),
  getGlobalChannel: () => req<NotificationChannel | null>("/notifications/admin/global-channel"),
  setGlobalChannel: (body: { kind: string; label?: string; config: Record<string, unknown>; enabled?: boolean }) =>
    req<NotificationChannel>("/notifications/admin/global-channel", { method: "PUT", body: JSON.stringify(body) }),
  broadcastNotification: (body: { kind: string; title: string; body: string }) =>
    req<{ recipients: number }>("/notifications/admin/broadcast", { method: "POST", body: JSON.stringify(body) }),

  // --- Per-user Goodreads (each user connects their own want-to-read shelf) ---
  getMyGoodreads: () => req<GoodreadsConnection>("/me/goodreads"),
  connectGoodreads: (body: { goodreads_user_id: string; shelf?: string; enabled?: boolean }) =>
    req<GoodreadsConnection>("/me/goodreads", { method: "PUT", body: JSON.stringify(body) }),
  syncGoodreads: () => req<GoodreadsConnection>("/me/goodreads/sync", { method: "POST" }),
  disconnectGoodreads: () =>
    req<{ disconnected: boolean }>("/me/goodreads", { method: "DELETE" }),
};
