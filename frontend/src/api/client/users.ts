// Users & auth domain: the current session (me), login/logout/setup, self-registration + password
// recovery, admin user management, and the admin-set defaults (category cap, permissions, 18+ gate).
import { req } from "./http";

export type RegistrationMode = "closed" | "open" | "approval";

export interface User {
  id: number;
  username: string;
  display_name: string | null;
  email: string | null;
  approval_status: "approved" | "pending";
  role: "admin" | "user";
  is_active: boolean;
  // Preferred UI language ("en" | "no"); null = follow the app/browser default.
  locale?: string | null;
  // Admin-set cap on viewable Index categories (null = inherit the global default).
  allowed_categories: string[] | null;
  // Admin-set capability flags (null = inherit the global default).
  permissions: string[] | null;
  created_at: string;
  // Derived from sessions (admin user-list only): newest session start + unexpired session count.
  last_seen?: string | null;
  active_sessions?: number;
}

export interface Me {
  authenticated: boolean;
  needs_setup: boolean;
  user: User | null;
  // Resolved categories the current user may view on the Index (admins → all).
  allowed_categories: string[];
  // Resolved capability flags the current user holds (admins → all). Drives the UI.
  permissions: string[];
  // Categories the admin permits 18+ content in (global gate; default all, empty = off everywhere).
  adult_allowed_categories: string[];
  // Resolved categories where this user sees 18+ content (inherits the full gate by default).
  adult_categories: string[];
}

export type Permission =
  | "index.view" | "index.hook" | "index.acquire" | "add.use"
  | "send.kindle" | "jobs.view" | "sources.view";

export const usersApi = {
  // --- Auth / users ---
  me: () => req<Me>("/auth/me"),
  // Self-service profile update (the Account tab). Changing the password needs current_password.
  updateMe: (body: {
    username?: string; display_name?: string; email?: string | null;
    password?: string; current_password?: string; locale?: string;
  }) => req<User>("/auth/me", { method: "PATCH", body: JSON.stringify(body) }),
  login: (username: string, password: string) =>
    req<User>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => req<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  setupAdmin: (username: string, password: string, displayName?: string) =>
    req<User>("/auth/setup", {
      method: "POST",
      body: JSON.stringify({ username, password, display_name: displayName }),
    }),
  // --- Self-registration + password recovery (all public, usable with no session) ---
  registrationMode: () => req<{ mode: RegistrationMode }>("/auth/registration-mode"),
  register: (body: { username: string; email: string; password: string; kindle_email?: string }) =>
    req<{ status: "ok" | "pending"; user: User | null }>("/auth/register", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  forgotPassword: (identifier: string) =>
    req<{ ok: boolean }>("/auth/forgot-password", {
      method: "POST",
      body: JSON.stringify({ identifier }),
    }),
  resetPassword: (token: string, password: string) =>
    req<{ ok: boolean }>("/auth/reset-password", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    }),
  listUsers: () => req<User[]>("/users"),
  createUser: (body: {
    username: string; password: string; role: string; display_name?: string; email?: string;
    allowed_categories?: string[] | null; permissions?: string[] | null;
  }) => req<User>("/users", { method: "POST", body: JSON.stringify(body) }),
  updateUser: (
    id: number,
    body: {
      username?: string; password?: string; role?: string; is_active?: boolean; display_name?: string; email?: string | null;
      allowed_categories?: string[] | null; permissions?: string[] | null;
    }
  ) => req<User>(`/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  // Hard-delete is protected: when the instance has a delete secret configured, it must be passed
  // (header X-User-Delete-Secret) or the server returns 403. Disabling (updateUser is_active=false)
  // is the unprotected, reversible alternative.
  deleteUser: (id: number, secret?: string) =>
    req<{ deleted: number }>(`/users/${id}`, {
      method: "DELETE",
      ...(secret ? { headers: { "X-User-Delete-Secret": secret } } : {}),
    }),
  userDeleteProtection: () => req<{ protected: boolean }>("/users/delete-protection"),
  // Admin: force sign-out everywhere (revoke all of a user's active sessions).
  logoutAllSessions: (id: number) =>
    req<{ revoked: number }>(`/users/${id}/logout-all`, { method: "POST" }),
  // Admin: approve / reject a self-registered user awaiting approval.
  approveUser: (id: number) => req<User>(`/users/${id}/approve`, { method: "POST" }),
  rejectUser: (id: number) => req<{ rejected: number }>(`/users/${id}/reject`, { method: "POST" }),
  // Admin: the default category cap for normal users (null = all).
  getCategoryDefault: () =>
    req<{ categories: string[] | null; all: string[] }>("/users/category-default"),
  setCategoryDefault: (categories: string[] | null) =>
    req<{ categories: string[] | null }>("/users/category-default", {
      method: "PUT",
      body: JSON.stringify({ categories }),
    }),
  // Admin: granular permission metadata + the default permission set for normal users.
  getPermissionsMeta: () =>
    req<{ all: { key: string; label: string }[]; default: string[]; baseline: string[] }>(
      "/users/permissions-meta"),
  setPermissionDefault: (permissions: string[] | null) =>
    req<{ permissions: string[] | null }>("/users/permission-default", {
      method: "PUT",
      body: JSON.stringify({ permissions }),
    }),
  // Admin: the global 18+ gate — which categories MAY surface adult content (empty = off).
  getAdultAllowed: () =>
    req<{ categories: string[]; all: string[] }>("/users/adult-allowed"),
  setAdultAllowed: (categories: string[]) =>
    req<{ categories: string[] }>("/users/adult-allowed", {
      method: "PUT",
      body: JSON.stringify({ categories }),
    }),
  // Self-service: the current user's per-category 18+ opt-in (bounded by the gate).
  setMyAdultCategories: (categories: string[]) =>
    req<{ adult_categories: string[]; effective: string[] }>("/auth/me/adult", {
      method: "PUT",
      body: JSON.stringify({ categories }),
    }),
};
