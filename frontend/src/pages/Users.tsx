import { type ReactNode, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MEDIA_CATEGORIES, RegistrationMode, User } from "../api/client";
import { qk } from "../api/queryKeys";
import { useCurrentUser, useAuth } from "../auth";
import {
  Badge, Button, Card, CardHeader, Chip, Disclosure, EmptyState, FormField, inputCls, Modal,
  SegmentedControl, Select, Spinner, StatusChip, Toggle,
} from "../components/ui";
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

/** Pick a set of media categories. `value === null` means "inherit / no restriction" (the
 *  toggle at the top); a list is an explicit selection. */
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
    <div className="space-y-2.5">
      <Toggle
        checked={inherit}
        onChange={(on) => onChange(on ? null : [...MEDIA_CATEGORIES])}
        label={inheritLabel}
      />
      {!inherit && (
        <div className="flex flex-wrap gap-1.5">
          {MEDIA_CATEGORIES.map((c) => {
            const on = set.has(c);
            return (
              <Chip
                key={c}
                active={on}
                onClick={() => {
                  const n = new Set(set);
                  on ? n.delete(c) : n.add(c);
                  onChange([...MEDIA_CATEGORIES].filter((x) => n.has(x)));
                }}
              >
                {on ? "✓ " : ""}
                {c}
              </Chip>
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
              <Chip
                key={c}
                active={on}
                onClick={() => {
                  const n = new Set(allowed);
                  on ? n.delete(c) : n.add(c);
                  save.mutate([...MEDIA_CATEGORIES].filter((x) => n.has(x)));
                }}
              >
                {on ? "✓ " : ""}
                {c}
              </Chip>
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
    <div className="space-y-2.5">
      <Toggle
        checked={inherit}
        onChange={(on) => onChange(on ? null : (meta.data?.default ?? []))}
        label={inheritLabel}
      />
      {!inherit && (
        <div className="divide-y divide-[var(--hair,var(--border))] rounded-xl border border-[var(--hair,var(--border))] bg-surface-2/40">
          {all.map((p) => {
            const on = set.has(p.key);
            return (
              <div key={p.key} className="flex items-center justify-between gap-3 px-3 py-2.5">
                <div className="min-w-0">
                  <div className="font-mono text-[11px] text-text">{p.key}</div>
                  <div className="text-xs text-muted">{p.label}</div>
                </div>
                <Toggle
                  checked={on}
                  onChange={() => {
                    const n = new Set(set);
                    on ? n.delete(p.key) : n.add(p.key);
                    onChange(all.map((x) => x.key).filter((k) => n.has(k)));
                  }}
                />
              </div>
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

/** The avatar initial disc, matching the ProviderCard avatar treatment. */
function Avatar({ name }: { name: string }) {
  return (
    <span className="font-display flex h-10 w-10 shrink-0 items-center justify-center rounded-[11px] bg-gradient-to-br from-[var(--accent)] to-[color-mix(in_srgb,var(--accent)_50%,#000)] text-[17px] font-semibold text-accent-fg">
      {name.slice(0, 1).toUpperCase()}
    </span>
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

/** The Users management surface — rendered as the admin "Users" sub-tab of Settings (no longer a
 *  standalone page). */
export function UsersPanel() {
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
    <div className="page-in max-w-3xl">
      <div className="mb-5 flex items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-xl font-semibold text-text">Users</h2>
          <p className="mt-0.5 text-sm text-muted">Everyone shares the same library; reading progress and settings are private to each account.</p>
        </div>
        <Button variant="primary" className="shrink-0" onClick={() => setDrawer({ mode: "create" })}>+ Add user</Button>
      </div>

      <DefaultsSection />

      {pendingCount > 0 && statusF !== "pending" && (
        <button
          type="button"
          onClick={() => setStatusF("pending")}
          className="mb-4 flex w-full items-center justify-between gap-3 rounded-2xl border border-[var(--hair,var(--border))] bg-[color-mix(in_srgb,var(--accent)_5%,var(--surface))] px-4 py-3 text-left transition hover:bg-surface-2"
        >
          <span className="flex items-center gap-2.5">
            <StatusChip tone="warning">{pendingCount} pending</StatusChip>
            <span className="text-sm text-text">
              user{pendingCount === 1 ? "" : "s"} awaiting approval
            </span>
          </span>
          <span className="shrink-0 text-xs font-semibold text-accent">Review →</span>
        </button>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search by name or email…"
          className={`${inputCls} min-w-[12rem] flex-1`}
        />
        <SegmentedControl
          ariaLabel="Filter by role"
          value={roleF}
          onChange={setRoleF}
          options={[
            { value: "all", label: "All" },
            { value: "admin", label: "Admins" },
            { value: "user", label: "Users" },
          ]}
        />
        <SegmentedControl
          ariaLabel="Filter by status"
          value={statusF}
          onChange={setStatusF}
          options={[
            { value: "all", label: "Any" },
            { value: "active", label: "Active" },
            { value: "disabled", label: "Disabled" },
            { value: "pending", label: "Pending" },
          ]}
        />
      </div>

      {users.isLoading ? (
        <Spinner label="Loading users…" />
      ) : all.length === 0 ? (
        <EmptyState title="No users yet" hint="Add the first account with “+ Add user”." />
      ) : filtered.length === 0 ? (
        <EmptyState title="No matching users" hint="Try a different search or filter." />
      ) : (
        <div className="space-y-2">
          {filtered.map((u) => (
            <Card key={u.id} className="p-0">
              <button
                onClick={() => setDrawer({ mode: "edit", userId: u.id })}
                className="flex w-full items-center gap-3 rounded-2xl px-4 py-3 text-left transition hover:bg-surface-2"
              >
                <Avatar name={u.display_name || u.username} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate font-semibold text-text">{u.display_name || u.username}</span>
                    {u.id === meUser?.id && <StatusChip tone="violet">you</StatusChip>}
                    {u.role === "admin" && <StatusChip tone="accent">admin</StatusChip>}
                    {u.approval_status === "pending" && <StatusChip tone="warning">pending</StatusChip>}
                    {!u.is_active && <StatusChip tone="danger">disabled</StatusChip>}
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
            </Card>
          ))}
        </div>
      )}

      {drawer?.mode === "create" && <UserDrawer mode="create" onClose={() => setDrawer(null)} />}
      {drawer?.mode === "edit" && editing && (
        <UserDrawer mode="edit" user={editing} isMe={editing.id === meUser?.id} onClose={() => setDrawer(null)} />
      )}
    </div>
  );
}

/** A hairline section card inside the user drawer — a small uppercase heading over its controls. */
function Section({ title, children, tone }: {
  title: ReactNode;
  children: ReactNode;
  tone?: "danger";
}) {
  return (
    <section
      className={`rounded-2xl border p-4 ${
        tone === "danger"
          ? "border-red-400/30 bg-red-500/[0.03]"
          : "border-[var(--hair,var(--border))] bg-surface-2/40"
      }`}
    >
      <div className={`mb-3 text-xs font-semibold uppercase tracking-wide ${tone === "danger" ? "text-red-500" : "text-muted"}`}>
        {title}
      </div>
      {children}
    </section>
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

  // Hard-delete protection: when the instance has a delete secret configured, the delete action
  // prompts for it (the flag is global, so this is fetched once and shared via react-query).
  const deleteProtected = useQuery({
    queryKey: ["user-delete-protection"],
    queryFn: api.userDeleteProtection,
    staleTime: 5 * 60_000,
  }).data?.protected ?? false;
  const deleteSecret = useRef("");

  const run = async (p: Promise<unknown>, ok?: string): Promise<boolean> => {
    setErr(null);
    setBusy(true);
    try {
      await p;
      refresh();
      if (ok) toast(ok, "success");
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
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
    <Modal
      title={title}
      variant="fullscreen-sheet"
      width="max-w-lg"
      onClose={props.onClose}
      footer={
        isCreate ? (
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={props.onClose}>Cancel</Button>
            <Button variant="primary" disabled={!username.trim() || pw.length < 8 || create.isPending}
              onClick={() => create.mutate()}>
              {create.isPending ? "Creating…" : "Create user"}
            </Button>
          </div>
        ) : (
          <div className="flex justify-between gap-2">
            <Button
              variant="danger"
              disabled={isMe}
              title={isMe ? "You can't delete your own account" : undefined}
              onClick={async () => {
                deleteSecret.current = "";
                const ok = await confirm({
                  title: "Delete user",
                  danger: true,
                  confirmText: "Delete",
                  message: deleteProtected ? (
                    <div className="space-y-2.5">
                      <p>Hard-delete user “{u!.username}” and all their data? This can't be undone.</p>
                      <p className="text-xs">Deletion is protected. Enter the delete secret to confirm — or set the account inactive instead (reversible).</p>
                      <input
                        type="password"
                        autoFocus
                        placeholder="Delete secret"
                        className={inputCls}
                        onChange={(e) => { deleteSecret.current = e.target.value; }}
                      />
                    </div>
                  ) : `Delete user “${u!.username}”? This can't be undone.`,
                });
                if (ok) {
                  const done = await run(
                    api.deleteUser(u!.id, deleteProtected ? deleteSecret.current : undefined),
                    "User deleted",
                  );
                  if (done) props.onClose();   // keep the drawer open on a wrong-secret 403 (err shown)
                }
              }}
            >
              Delete user
            </Button>
            <Button variant="ghost" onClick={props.onClose}>Done</Button>
          </div>
        )
      }
    >
      {err && <p className="mb-4 rounded-xl border border-red-400/30 bg-red-500/10 px-3 py-2 text-sm text-red-500">{err}</p>}

      {!isCreate && (
        <div className="mb-4 flex items-center gap-3">
          <Avatar name={u!.display_name || u!.username} />
          <div className="min-w-0">
            <div className="truncate font-semibold text-text">{u!.display_name || u!.username}</div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {isMe && <StatusChip tone="violet">you</StatusChip>}
              {u!.role === "admin" && <StatusChip tone="accent">admin</StatusChip>}
              {u!.approval_status === "pending" && <StatusChip tone="warning">pending</StatusChip>}
              {!u!.is_active && <StatusChip tone="danger">disabled</StatusChip>}
            </div>
          </div>
        </div>
      )}

      <div className="space-y-4">
        <Section title="Identity">
          <FormField label="Username" hint={isCreate ? undefined : "The login name used to sign in (must be unique)."}>
            <input className={inputCls} value={username} onChange={(e) => setUsername(e.target.value)}
              placeholder="username" autoFocus={isCreate} />
          </FormField>
          <FormField label="Display name">
            <input className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)}
              placeholder={isCreate ? "(optional)" : u!.username} />
          </FormField>
          <FormField label="Email" hint="Used for password recovery.">
            <input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)}
              placeholder="(optional)" />
          </FormField>
          {!isCreate && (
            <Button
              variant="primary"
              disabled={busy || !username.trim() || (
                username.trim() === u!.username &&
                displayName.trim() === (u!.display_name ?? "") &&
                email.trim() === (u!.email ?? "")
              )}
              onClick={() =>
                run(
                  api.updateUser(u!.id, {
                    username: username.trim(),
                    display_name: displayName.trim(),
                    email: email.trim() || null,
                  }),
                  "Profile saved"
                )
              }
            >
              Save profile
            </Button>
          )}
        </Section>

        {/* Role + categories/permissions. Admins implicitly hold everything, so the pickers only
            show for regular users. */}
        <Section title="Role & access">
          {isCreate ? (
            <FormField label="Role">
              <Select value={role} onChange={setRole}
                options={[{ value: "user", label: "User" }, { value: "admin", label: "Admin" }]} />
            </FormField>
          ) : (
            <div className="mb-3.5 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-sm">
                <span className="text-muted">Role</span>
                <StatusChip tone={u!.role === "admin" ? "accent" : "neutral"}>{u!.role}</StatusChip>
              </div>
              <Button
                size="sm"
                variant="outline"
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
            <div className="grid gap-4">
              <div>
                <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Permissions</div>
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
                <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Viewable categories</div>
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
        </Section>

        {/* Password */}
        <Section title={isCreate ? "Password" : "Set a new password"}>
          <input className={inputCls} type="password" value={pw} onChange={(e) => setPw(e.target.value)}
            placeholder={isCreate ? "at least 8 characters" : "at least 8 characters"} autoComplete="new-password" />
          {!isCreate && (
            <Button className="mt-2.5" size="sm" variant="outline" disabled={pw.length < 8 || busy}
              onClick={() => run(api.updateUser(u!.id, { password: pw }), "Password updated").then(() => setPw(""))}>
              Set password
            </Button>
          )}
        </Section>

        {/* Account actions (edit only) */}
        {!isCreate && (
          <Section title="Danger zone" tone="danger">
            {u!.approval_status === "pending" && (
              <div className="mb-3 flex gap-2">
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
                variant="outline"
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
                variant="outline"
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
            <p className="mt-3 text-xs text-muted">
              Last sign-in {fmtAgo(u!.last_seen)}
              {u!.active_sessions ? ` · ${u!.active_sessions} active session${u!.active_sessions === 1 ? "" : "s"}` : ""}.
            </p>
          </Section>
        )}
      </div>
    </Modal>
  );
}
