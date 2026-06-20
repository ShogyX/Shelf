// Following domain: a user's follows of an author or series (Wave E). The follow_tick auto-fetches
// new titles for an active sub with auto_request on.
import { req } from "./http";

export interface Subscription {
  id: number;
  kind: "author" | "series";
  key: string;
  display_name: string;
  active: boolean;
  auto_request: boolean;
  auto_added: number;
  last_checked_at: string | null;
  created_at: string | null;
}

export const subscriptionsApi = {
  listSubscriptions: () => req<Subscription[]>("/subscriptions"),
  // Follow the author or series of a catalog row (series can also be named directly).
  follow: (body: { kind: "author" | "series"; catalog_id?: number; series_name?: string }) =>
    req<Subscription>("/subscriptions", { method: "POST", body: JSON.stringify(body) }),
  patchSubscription: (id: number, body: { auto_request?: boolean; active?: boolean }) =>
    req<Subscription>(`/subscriptions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  unfollow: (id: number) =>
    req<{ deleted: number }>(`/subscriptions/${id}`, { method: "DELETE" }),
};
