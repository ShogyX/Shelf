import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MEDIA_CATEGORIES, RegistrationMode, User } from "../api/client";
import { qk } from "../api/queryKeys";
import { useCurrentUser, useAuth } from "../auth";
import { Badge, Button, Card, CardHeader, Disclosure, EmptyState, inputCls, Modal, Select, Spinner } from "../components/ui";
import { useConfirm } from "../components/confirm";
import { useApp } from "../store";
import { SystemConfigCard } from "../components/SystemSettings";

/** Admin: who can create an account. Stored in the shared system-config under "registration_mode"
 *  via the same merge-PUT the backup/system settings use. */
function RegistrationModeCard() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: qk.systemConfig(), queryFn: api.getSystemConfig });
  const save = useMutation({
    mutationFn: (mode: RegistrationMode) => api.putSystemConfig({ registration_mode: mode }),
    onSuccess: (d) => qc.setQueryData(qk.systemConfig(), d),
  });
  const mode = (cfg.data?.values.registration_mode as RegistrationMode | undefined) ?? "closed";
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title="Self-registration"
        desc="Who can create an account from the sign-in screen."
        hint={<><b>Closed</b> — only admins add users. <b>Open</b> — anyone can sign up and use the app
          immediately. <b>Approval</b> — anyone can sign up, but an admin must approve them before they
          can sign in.</>}
      />
      {cfg.isLoading ? (
        <Spinner label="Loading…" />
      ) : (
        <Select
          value={mode}
          onChange={(v) => save.mutate(v as RegistrationMode)}
          options={[
            { value: "closed", label: "Closed — admins create accounts" },
            { value: "open", label: "Open — anyone can sign up" },
            { value: "approval", label: "Approval — sign-ups need admin approval" },
          ]}
        />
      )}
      {save.isPending && <p className="mt-1 text-xs text-accent">Saving…</p>}
      {save.isError && <p className="mt-1 text-xs text-red-500">{(save.error as Error).message}</p>}
    </Card>
  );
}

const chip = (on: boolean) =>
  `rounded-full border px-2.5 py-1 text-xs transition ${
    on ? "border-accent bg-accent text-accent-fg" : "border-border bg-surface text-muted hover:bg-surface-2"
  }`;

/** Pick a set of media categories. `value === null` means "inherit / no restriction" (the
 *  checkbox at the top); a list is an explicit selection. */
function CategoryPicker({
  value,
  onChange,
  inheritLabel,
}: {
  value: string[] | null;
  onChange: (v: string[] | null) => void;
  inheritLabel: string;
}) {
  const inherit = value === null;
  const set = new Set(value ?? []);
  return (
    <div className="space-y-1.5">
      <label className="flex items-center gap-2 text-sm text-text">
        <input
          type="checkbox"
          checked={inherit}
          onChange={(e) => onChange(e.target.checked ? null : [...MEDIA_CATEGORIES])}
        />
        {inheritLabel}
      </label>
      {!inherit && (
        <div className="flex flex-wrap gap-1.5">
          {MEDIA_CATEGORIES.map((c) => {
            const on = set.has(c);
            return (
              <button
                key={c}
                type="button"
                onClick={() => {
                  const n = new Set(set);
                  on ? n.delete(c) : n.add(c);
                  onChange([...MEDIA_CATEGORIES].filter((x) => n.has(x)));
                }}
                className={chip(on)}
              >
                {on ? "✓ " : ""}
                {c}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Admin: the category cap applied to normal users who have no per-user cap. */
function DefaultCategoriesCard() {
  const qc = useQueryClient();
  const def = useQuery({ queryKey: qk.categoryDefault(), queryFn: api.getCategoryDefault });
  const save = useMutation({
    mutationFn: (cats: string[] | null) => api.setCategoryDefault(cats),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.categoryDefault() }),
  });
  // null from the API = no restriction (all). The picker's "inherit" checkbox here means "all".
  const value = def.data?.categories ?? null;
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title="Default categories"
        desc="Categories non-admins can view without a per-user override."
        hint="Applies to any non-admin user who has no per-user category cap. Admins always see every category."
      />
      {def.isLoading ? (
        <Spinner label="Loading…" />
      ) : (
        <CategoryPicker
          value={value}
          inheritLabel="All categories (no restriction)"
          onChange={(v) => save.mutate(v)}
        />
      )}
      {save.isPending && <p className="mt-1 text-xs text-accent">Saving…</p>}
    </Card>
  );
}

/** Admin: the global 18+ gate — which categories MAY surface adult content at all. Off by default;
 *  even where enabled, each user must still opt in for themselves under their own settings. */
function AdultGateCard() {
  const qc = useQueryClient();
  const refreshMe = useAuth((s) => s.refresh);
  const gate = useQuery({ queryKey: qk.adultAllowed(), queryFn: api.getAdultAllowed });
  const save = useMutation({
    mutationFn: (cats: string[]) => api.setAdultAllowed(cats),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.adultAllowed() });
      refreshMe();                                  // the gate bounds every user's opt-in (incl. mine)
      qc.invalidateQueries({ queryKey: qk.catalogRows() });
    },
  });
  const allowed = new Set(gate.data?.categories ?? []);
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title="Adult content (18+)"
        badge={<Badge tone="red">18+</Badge>}
        desc="Which categories may surface explicit 18+ content."
        hint="Enabled per category by default; turn one off to hide its 18+ content for everyone, or leave all off to disable 18+ entirely. Each user can still narrow this further under their own settings."
      />
      {gate.isLoading ? (
        <Spinner label="Loading…" />
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {MEDIA_CATEGORIES.map((c) => {
            const on = allowed.has(c);
            return (
              <button
                key={c}
                type="button"
                onClick={() => {
                  const n = new Set(allowed);
                  on ? n.delete(c) : n.add(c);
                  save.mutate([...MEDIA_CATEGORIES].filter((x) => n.has(x)));
                }}
                className={chip(on)}
              >
                {on ? "✓ " : ""}
                {c}
              </button>
            );
          })}
        </div>
      )}
      {save.isPending && <p className="mt-1 text-xs text-accent">Saving…</p>}
    </Card>
  );
}

/** Pick a set of capability flags. `value === null` = inherit the global default. Options + labels
 *  come from the server (permissions-meta), so this never drifts from the backend taxonomy. */
function PermissionPicker({
  value,
  onChange,
  inheritLabel,
}: {
  value: string[] | null;
  onChange: (v: string[] | null) => void;
  inheritLabel: string;
}) {
  const meta = useQuery({ queryKey: qk.permissionsMeta(), queryFn: api.getPermissionsMeta });
  const all = meta.data?.all ?? [];
  const inherit = value === null;
  const set = new Set(value ?? []);
  return (
    <div className="space-y-1.5">
      <label className="flex items-center gap-2 text-sm text-text">
        <input
          type="checkbox"
          checked={inherit}
          onChange={(e) => onChange(e.target.checked ? null : (meta.data?.default ?? []))}
        />
        {inheritLabel}
      </label>
      {!inherit && (
        <div className="flex flex-col gap-1.5">
          {all.map((p) => {
            const on = set.has(p.key);
            return (
              <button
                key={p.key}
                type="button"
                onClick={() => {
                  const n = new Set(set);
                  on ? n.delete(p.key) : n.add(p.key);
                  onChange(all.map((x) => x.key).filter((k) => n.has(k)));
                }}
                className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-left text-xs transition ${
                  on ? "border-accent bg-accent/10 text-text" : "border-border bg-surface text-muted hover:bg-surface-2"
                }`}
              >
                <span className={`shrink-0 ${on ? "text-accent" : "text-muted"}`}>{on ? "☑" : "☐"}</span>
                <span className="font-mono text-[11px]">{p.key}</span>
                <span className="text-muted">— {p.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Admin: the capability set granted to normal users who have no per-user override. */
function DefaultPermissionsCard() {
  const qc = useQueryClient();
  const meta = useQuery({ queryKey: qk.permissionsMeta(), queryFn: api.getPermissionsMeta });
  const save = useMutation({
    mutationFn: (perms: string[] | null) => api.setPermissionDefault(perms),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.permissionsMeta() }),
  });
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title="Default permissions"
        desc="What non-admins can see and do without a per-user override."
        hint="Managing sources, jobs, the crawler, integrations and backups always stays admin-only. Admins have everything."
      />
      {meta.isLoading ? (
        <Spinner label="Loading…" />
      ) : (
        <PermissionPicker
          value={meta.data?.default ?? []}
          inheritLabel="Reset to the built-in baseline"
          onChange={(v) => save.mutate(v)}
        />
      )}
      {save.isPending && <p className="mt-1 text-xs text-accent">Saving…</p>}
    </Card>
  );
}

/** Relative "last seen" from a session timestamp (browser-side; no stored last_login). */
function fmtAgo(iso?: string | null): string {
  if (!iso) return "never signed in";
  const d = new Date(iso);
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 604800) return `${Math.floor(s / 86400)}d ago`;
  return d.toLocaleDateString();
}

/** The admin "defaults" cards, collapsed by default so they don't dominate the page above the
 *  actual user list. */
function DefaultsSection() {
  return (
    <Disclosure
      title="Registration & defaults"
      subtitle="Who can sign up, login security, and what new users inherit"
    >
      <RegistrationModeCard />
      <SystemConfigCard groups={["Login & security"]} />
      <DefaultPermissionsCard />
      <DefaultCategoriesCard />
      <AdultGateCard />
    </Disclosure>
  );
}

export default function Users() {
  const meUser = useCurrentUser();
  const users = useQuery({ queryKey: qk.users(), queryFn: api.listUsers });
  const [q, setQ] = useState("");
  const [roleF, setRoleF] = useState("all");
  const [statusF, setStatusF] = useState("all");
  const [drawer, setDrawer] = useState<{ mode: "create" } | { mode: "edit"; userId: number } | null>(null);

  const all = users.data ?? [];
  const pendingCount = all.filter((u) => u.approval_status === "pending").length;
  const needle = q.trim().toLowerCase();
  const filtered = all.filter((u) => {
    if (roleF !== "all" && u.role !== roleF) return false;
    if (statusF === "pending" && u.approval_status !== "pending") return false;
    if (statusF === "active" && (!u.is_active || u.approval_status === "pending")) return false;
    if (statusF === "disabled" && u.is_active) return false;
    if (needle && ![u.username, u.display_name, u.email].some((v) => v?.toLowerCase().includes(needle)))
      return false;
    return true;
  });
  // The drawer reads the live user from the query so edits reflect immediately after invalidation.
  const editing = drawer?.mode === "edit" ? all.find((u) => u.id === drawer.userId) ?? null : null;

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Users</h1>
        <Button variant="primary" onClick={() => setDrawer({ mode: "create" })}>+ Add user</Button>
      </div>
      <p className="mb-5 text-sm text-muted">
        Everyone shares the same library; reading progress and settings are private to each account.
      </p>

      <DefaultsSection />

      {pendingCount > 0 && statusF !== "pending" && (
        <button
          type="button"
          onClick={() => setStatusF("pending")}
          className="mb-3 flex w-full items-center justify-between rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-left text-sm"
        >
          <span className="text-amber-600 dark:text-amber-400">
            {pendingCount} user{pendingCount === 1 ? "" : "s"} awaiting approval
          </span>
          <span className="text-xs text-muted">Review →</span>
        </button>
      )}

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search by name or email…"
          className={`${inputCls} min-w-[12rem] flex-1`}
        />
        <select value={roleF} onChange={(e) => setRoleF(e.target.value)} className={`${inputCls} w-auto!`}>
          <option value="all">All roles</option>
          <option value="admin">Admins</option>
          <option value="user">Users</option>
        </select>
        <select value={statusF} onChange={(e) => setStatusF(e.target.value)} className={`${inputCls} w-auto!`}>
          <option value="all">Any status</option>
          <option value="active">Active</option>
          <option value="disabled">Disabled</option>
          <option value="pending">Pending</option>
        </select>
      </div>

      {users.isLoading ? (
        <Spinner label="Loading users…" />
      ) : all.length === 0 ? (
        <EmptyState title="No users yet" hint="Add the first account with “+ Add user”." />
      ) : filtered.length === 0 ? (
        <EmptyState title="No matching users" hint="Try a different search or filter." />
      ) : (
        <div className="space-y-1.5">
          {filtered.map((u) => (
            <button
              key={u.id}
              onClick={() => setDrawer({ mode: "edit", userId: u.id })}
              className="flex w-full items-center gap-3 rounded-lg border border-border bg-surface px-3 py-2.5 text-left transition hover:bg-surface-2"
            >
              <div className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-accent/10 text-sm font-semibold text-accent">
                {(u.display_name || u.username).slice(0, 1).toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="truncate font-medium">{u.display_name || u.username}</span>
                  {u.id === meUser?.id && <Badge tone="violet">you</Badge>}
                  <Badge tone={u.role === "admin" ? "amber" : "default"}>{u.role}</Badge>
                  {u.approval_status === "pending" && <Badge tone="amber">pending</Badge>}
                  {!u.is_active && <Badge tone="red">disabled</Badge>}
                </div>
                <div className="truncate text-xs text-muted">
                  {u.display_name ? `@${u.username}` : ""}
                  {u.email ? `${u.display_name ? " · " : ""}${u.email}` : ""}
                </div>
              </div>
              <div className="shrink-0 text-right text-xs text-muted">
                <div>{fmtAgo(u.last_seen)}</div>
                {!!u.active_sessions && (
                  <div>{u.active_sessions} session{u.active_sessions === 1 ? "" : "s"}</div>
                )}
              </div>
            </button>
          ))}
        </div>
      )}

      {drawer?.mode === "create" && <UserDrawer mode="create" onClose={() => setDrawer(null)} />}
      {drawer?.mode === "edit" && editing && (
        <UserDrawer mode="edit" user={editing} isMe={editing.id === meUser?.id} onClose={() => setDrawer(null)} />
      )}
    </main>
  );
}

/** The single edit/create surface for a user. Profile fields (name/email/password) save together;
 *  role, status, permissions, categories, force-logout and delete are immediate actions. */
function UserDrawer(
  props:
    | { mode: "create"; onClose: () => void }
    | { mode: "edit"; user: User; isMe: boolean; onClose: () => void }
) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const toast = useApp((s) => s.toast);
  const refresh = () => qc.invalidateQueries({ queryKey: qk.users() });

  const isCreate = props.mode === "create";
  const u = props.mode === "edit" ? props.user : null;
  const isMe = props.mode === "edit" ? props.isMe : false;

  const [username, setUsername] = useState(u?.username ?? "");
  const [displayName, setDisplayName] = useState(u?.display_name ?? "");
  const [email, setEmail] = useState(u?.email ?? "");
  const [role, setRole] = useState<string>(u?.role ?? "user");
  const [pw, setPw] = useState("");
  const [cats, setCats] = useState<string[] | null>(u?.allowed_categories ?? null);
  const [perms, setPerms] = useState<string[] | null>(u?.permissions ?? null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Re-seed ONLY when the drawer switches to a different user (key on id), NOT on every server field
  // change — otherwise a sibling action's refetch (e.g. "Make admin" flips u.role) would silently
  // revert the name/email the admin just typed but hasn't saved (CODE-M3).
  useEffect(() => {
    if (!u) return;
    setDisplayName(u.display_name ?? "");
    setEmail(u.email ?? "");
    setRole(u.role);
  }, [u?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (p: Promise<unknown>, ok?: string) => {
    setErr(null);
    setBusy(true);
    try {
      await p;
      refresh();
      if (ok) toast(ok, "success");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const create = useMutation({
    mutationFn: () =>
      api.createUser({
        username: username.trim(),
        password: pw,
        role,
        display_name: displayName.trim() || undefined,
        email: email.trim() || undefined,
        allowed_categories: role === "admin" ? null : cats,
        permissions: role === "admin" ? null : perms,
      }),
    onSuccess: () => {
      refresh();
      toast(`Created “${username.trim()}”`, "success");
      props.onClose();
    },
    onError: (e) => setErr((e as Error).message),
  });

  const title = isCreate ? "Add a user" : (u!.display_name || u!.username);

  return (
    <Modal title={title} variant="sheet" width="w-[30rem]" onClose={props.onClose}>
      {err && <p className="mb-3 rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-500">{err}</p>}

      <div className="space-y-4">
        <section className="space-y-2">
          {isCreate && (
            <label className="block">
              <div className="mb-1 text-xs text-muted">Username</div>
              <input className={inputCls} value={username} onChange={(e) => setUsername(e.target.value)}
                placeholder="username" autoFocus />
            </label>
          )}
          <label className="block">
            <div className="mb-1 text-xs text-muted">Display name</div>
            <input className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)}
              placeholder={isCreate ? "(optional)" : u!.username} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-muted">Email <span className="text-muted/70">— for password recovery</span></div>
            <input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)}
              placeholder="(optional)" />
          </label>
          {!isCreate && (
            <Button
              variant="primary"
              disabled={busy || (displayName.trim() === (u!.display_name ?? "") && email.trim() === (u!.email ?? ""))}
              onClick={() =>
                run(
                  api.updateUser(u!.id, { display_name: displayName.trim(), email: email.trim() || null }),
                  "Profile saved"
                )
              }
            >
              Save profile
            </Button>
          )}
        </section>

        {/* Role + categories/permissions. Admins implicitly hold everything, so the pickers only
            show for regular users. */}
        <section className="space-y-2 border-t border-border pt-4">
          {isCreate ? (
            <label className="block">
              <div className="mb-1 text-xs text-muted">Role</div>
              <Select value={role} onChange={setRole}
                options={[{ value: "user", label: "User" }, { value: "admin", label: "Admin" }]} />
            </label>
          ) : (
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm">
                Role: <span className="font-medium">{u!.role}</span>
              </div>
              <Button
                size="sm"
                variant="ghost"
                disabled={isMe || busy}
                title={isMe ? "You can't change your own role" : undefined}
                onClick={async () => {
                  const toAdmin = u!.role !== "admin";
                  if (
                    await confirm({
                      title: toAdmin ? "Grant admin" : "Revoke admin",
                      message: toAdmin
                        ? `Make “${u!.username}” an admin? They'll get full control of this instance.`
                        : `Demote “${u!.username}” to a regular user?`,
                      danger: toAdmin,
                    })
                  )
                    run(api.updateUser(u!.id, { role: toAdmin ? "admin" : "user" }));
                }}
              >
                {u!.role === "admin" ? "Make user" : "Make admin"}
              </Button>
            </div>
          )}

          {/* Edit mode reads LIVE u!.role (a "Make admin" with the drawer open flips it without a
              re-seed); create mode uses local role state. Keeps the pickers' visibility correct. */}
          {(isCreate ? role : u!.role) !== "admin" && (
            <div className="grid gap-4 pt-1">
              <div>
                <div className="mb-1 text-xs uppercase tracking-wide text-muted">Permissions</div>
                {/* Edit mode reads the LIVE user so a server-normalized value (or a concurrent
                    change) is reflected; create mode uses local state seeded to null. */}
                <PermissionPicker
                  value={isCreate ? perms : u!.permissions}
                  inheritLabel="Inherit the default for normal users"
                  onChange={(v) => {
                    if (isCreate) setPerms(v);
                    else run(api.updateUser(u!.id, { permissions: v }));
                  }}
                />
              </div>
              <div>
                <div className="mb-1 text-xs uppercase tracking-wide text-muted">Viewable categories</div>
                <CategoryPicker
                  value={isCreate ? cats : u!.allowed_categories}
                  inheritLabel="Inherit the default for normal users"
                  onChange={(v) => {
                    if (isCreate) setCats(v);
                    else run(api.updateUser(u!.id, { allowed_categories: v }));
                  }}
                />
              </div>
            </div>
          )}
        </section>

        {/* Password */}
        <section className="space-y-2 border-t border-border pt-4">
          <div className="text-xs uppercase tracking-wide text-muted">{isCreate ? "Password" : "Set a new password"}</div>
          <input className={inputCls} type="password" value={pw} onChange={(e) => setPw(e.target.value)}
            placeholder={isCreate ? "at least 8 characters" : "at least 8 characters"} autoComplete="new-password" />
          {!isCreate && (
            <Button size="sm" variant="outline" disabled={pw.length < 8 || busy}
              onClick={() => run(api.updateUser(u!.id, { password: pw }), "Password updated").then(() => setPw(""))}>
              Set password
            </Button>
          )}
        </section>

        {/* Account actions (edit only) */}
        {!isCreate && (
          <section className="space-y-2 border-t border-border pt-4">
            {u!.approval_status === "pending" && (
              <div className="flex gap-2">
                <Button size="sm" variant="primary" disabled={busy} onClick={() => run(api.approveUser(u!.id), "User approved")}>
                  Approve
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={async () => {
                    if (await confirm({ title: "Reject user", message: `Reject and delete “${u!.username}”’s pending registration?`, danger: true, confirmText: "Reject" })) {
                      await run(api.rejectUser(u!.id), "Registration rejected");
                      props.onClose();
                    }
                  }}
                >
                  Reject
                </Button>
              </div>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="ghost"
                disabled={isMe || busy}
                onClick={async () => {
                  if (
                    await confirm({
                      title: u!.is_active ? "Disable user" : "Enable user",
                      message: u!.is_active
                        ? `Disable “${u!.username}”? They won't be able to sign in.`
                        : `Re-enable “${u!.username}”?`,
                      danger: u!.is_active,
                    })
                  )
                    run(api.updateUser(u!.id, { is_active: !u!.is_active }));
                }}
              >
                {u!.is_active ? "Disable account" : "Enable account"}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={isMe || busy || !u!.active_sessions}
                title={
                  isMe
                    ? "You can't sign yourself out here"
                    : u!.active_sessions
                      ? `Sign out of ${u!.active_sessions} active session(s)`
                      : "No active sessions"
                }
                onClick={async () => {
                  if (await confirm({ title: "Log out everywhere", message: `Sign “${u!.username}” out of all devices? They'll need to sign in again.`, confirmText: "Log out" }))
                    run(api.logoutAllSessions(u!.id), "Signed out everywhere");
                }}
              >
                Log out everywhere
              </Button>
            </div>
            <p className="text-xs text-muted">
              Last sign-in {fmtAgo(u!.last_seen)}
              {u!.active_sessions ? ` · ${u!.active_sessions} active session${u!.active_sessions === 1 ? "" : "s"}` : ""}.
            </p>
          </section>
        )}
      </div>

      <div className="mt-6 flex justify-between gap-2 border-t border-border pt-4">
        {isCreate ? (
          <>
            <Button variant="ghost" onClick={props.onClose}>Cancel</Button>
            <Button variant="primary" disabled={!username.trim() || pw.length < 8 || create.isPending}
              onClick={() => create.mutate()}>
              {create.isPending ? "Creating…" : "Create user"}
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="danger"
              disabled={isMe}
              title={isMe ? "You can't delete your own account" : undefined}
              onClick={async () => {
                if (await confirm({ title: "Delete user", message: `Delete user “${u!.username}”? This can't be undone.`, danger: true })) {
                  await run(api.deleteUser(u!.id), "User deleted");
                  props.onClose();
                }
              }}
            >
              Delete user
            </Button>
            <Button variant="ghost" onClick={props.onClose}>Done</Button>
          </>
        )}
      </div>
    </Modal>
  );
}
