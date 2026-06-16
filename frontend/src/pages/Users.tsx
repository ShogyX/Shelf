import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MEDIA_CATEGORIES, RegistrationMode, User } from "../api/client";
import { useCurrentUser, useAuth } from "../auth";
import { Badge, Button, Card, EmptyState, Select, Spinner } from "../components/ui";
import { useConfirm } from "../components/confirm";
import { SystemConfigCard } from "../components/SystemSettings";

/** Admin: who can create an account. Stored in the shared system-config under "registration_mode"
 *  via the same merge-PUT the backup/system settings use. */
function RegistrationModeCard() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["system-config"], queryFn: api.getSystemConfig });
  const save = useMutation({
    mutationFn: (mode: RegistrationMode) => api.putSystemConfig({ registration_mode: mode }),
    onSuccess: (d) => qc.setQueryData(["system-config"], d),
  });
  const mode = (cfg.data?.values.registration_mode as RegistrationMode | undefined) ?? "closed";
  return (
    <Card className="mb-6 p-4">
      <div className="mb-1 text-sm font-medium">Self-registration</div>
      <p className="mb-2 text-sm text-muted">
        Whether visitors can create their own account from the sign-in screen. <b>Closed</b> — only
        admins add users. <b>Open</b> — anyone can sign up and use the app immediately. <b>Approval</b>
        {" "}— anyone can sign up, but an admin must approve them below before they can sign in.
      </p>
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
  const def = useQuery({ queryKey: ["category-default"], queryFn: api.getCategoryDefault });
  const save = useMutation({
    mutationFn: (cats: string[] | null) => api.setCategoryDefault(cats),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["category-default"] }),
  });
  // null from the API = no restriction (all). The picker's "inherit" checkbox here means "all".
  const value = def.data?.categories ?? null;
  return (
    <Card className="mb-6 p-4">
      <div className="mb-1 text-sm font-medium">Default categories for new / normal users</div>
      <p className="mb-2 text-sm text-muted">
        Applies to any non-admin user without their own per-user override below. Admins always see
        every category.
      </p>
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
  const gate = useQuery({ queryKey: ["adult-allowed"], queryFn: api.getAdultAllowed });
  const save = useMutation({
    mutationFn: (cats: string[]) => api.setAdultAllowed(cats),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["adult-allowed"] });
      refreshMe();                                  // the gate bounds every user's opt-in (incl. mine)
      qc.invalidateQueries({ queryKey: ["catalog-rows"] });
    },
  });
  const allowed = new Set(gate.data?.categories ?? []);
  return (
    <Card className="mb-6 p-4">
      <div className="mb-1 flex items-center gap-2 text-sm font-medium">
        Adult content (18+) <Badge tone="red">18+</Badge>
      </div>
      <p className="mb-2 text-sm text-muted">
        Choose which categories may surface explicit 18+ content. Enabled for every category by
        default; turn one off to hide its 18+ content for everyone, or leave all off to disable 18+
        entirely. Each user can still narrow this further under their own settings.
      </p>
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
  const meta = useQuery({ queryKey: ["permissions-meta"], queryFn: api.getPermissionsMeta });
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
  const meta = useQuery({ queryKey: ["permissions-meta"], queryFn: api.getPermissionsMeta });
  const save = useMutation({
    mutationFn: (perms: string[] | null) => api.setPermissionDefault(perms),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["permissions-meta"] }),
  });
  return (
    <Card className="mb-6 p-4">
      <div className="mb-1 text-sm font-medium">Default permissions for new / normal users</div>
      <p className="mb-2 text-sm text-muted">
        What a non-admin can see and do when they have no per-user override below. Managing sources,
        jobs, the crawler, integrations and backups always stays admin-only. Admins have everything.
      </p>
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

export default function Users() {
  const qc = useQueryClient();
  const meUser = useCurrentUser();
  const confirm = useConfirm();
  const users = useQuery({ queryKey: ["users"], queryFn: api.listUsers });
  const [error, setError] = useState<string | null>(null);

  const [nu, setNu] = useState("");
  const [np, setNp] = useState("");
  const [nrole, setNrole] = useState("user");
  const [ncats, setNcats] = useState<string[] | null>(null); // null = inherit default
  const [nperms, setNperms] = useState<string[] | null>(null); // null = inherit default

  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });
  const wrap = (p: Promise<unknown>) => p.then(refresh).catch((e) => setError((e as Error).message));

  const create = useMutation({
    mutationFn: () =>
      api.createUser({ username: nu.trim(), password: np, role: nrole,
                       allowed_categories: ncats, permissions: nperms }),
    onSuccess: () => { setNu(""); setNp(""); setNrole("user"); setNcats(null); setNperms(null); setError(null); refresh(); },
    onError: (e) => setError((e as Error).message),
  });

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Users</h1>
      <p className="mb-6 text-sm text-muted">
        Everyone shares the same library; reading progress and settings are private to each account.
      </p>

      <RegistrationModeCard />
      <SystemConfigCard groups={["Login & security"]} />
      <DefaultPermissionsCard />
      <DefaultCategoriesCard />
      <AdultGateCard />

      <Card className="mb-6 p-4">
        <div className="mb-3 text-sm font-medium">Add a user</div>
        <div className="grid gap-2 sm:grid-cols-4">
          <input value={nu} onChange={(e) => setNu(e.target.value)} placeholder="username"
            className="rounded-lg border border-border bg-bg px-3 py-2 text-sm sm:col-span-1" />
          <input value={np} onChange={(e) => setNp(e.target.value)} placeholder="password" type="password"
            className="rounded-lg border border-border bg-bg px-3 py-2 text-sm sm:col-span-1" />
          <select value={nrole} onChange={(e) => setNrole(e.target.value)}
            className="rounded-lg border border-border bg-bg px-3 py-2 text-sm">
            <option value="user">User</option>
            <option value="admin">Admin</option>
          </select>
          <Button variant="primary" disabled={!nu.trim() || np.length < 4 || create.isPending}
            onClick={() => create.mutate()}>
            {create.isPending ? "Adding…" : "Add user"}
          </Button>
        </div>
        {nrole !== "admin" && (
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <div>
              <div className="mb-1 text-xs uppercase tracking-wide text-muted">Permissions</div>
              <PermissionPicker value={nperms} inheritLabel="Inherit the default above" onChange={setNperms} />
            </div>
            <div>
              <div className="mb-1 text-xs uppercase tracking-wide text-muted">Viewable categories</div>
              <CategoryPicker value={ncats} inheritLabel="Inherit the default above" onChange={setNcats} />
            </div>
          </div>
        )}
        {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
      </Card>

      {users.isLoading ? (
        <Spinner label="Loading users…" />
      ) : (users.data?.length ?? 0) === 0 ? (
        <EmptyState title="No users yet" />
      ) : (
        <div className="space-y-2">
          {users.data!.map((u) => (
            <UserRow key={u.id} u={u} isMe={u.id === meUser?.id}
              onChange={(p) => wrap(api.updateUser(u.id, p))}
              onApprove={() => wrap(api.approveUser(u.id))}
              onReject={async () => {
                if (await confirm({ title: "Reject user", message: `Reject and delete “${u.username}”’s pending registration?`, danger: true, confirmText: "Reject" }))
                  wrap(api.rejectUser(u.id));
              }}
              onDelete={async () => {
                if (await confirm({ title: "Delete user", message: `Delete user “${u.username}”? This can't be undone.`, danger: true }))
                  wrap(api.deleteUser(u.id));
              }} />
          ))}
        </div>
      )}
    </main>
  );
}

function UserRow({ u, isMe, onChange, onApprove, onReject, onDelete }: {
  u: User; isMe: boolean;
  onChange: (patch: {
    role?: string; is_active?: boolean; password?: string;
    allowed_categories?: string[] | null; permissions?: string[] | null;
  }) => void;
  onApprove: () => void;
  onReject: () => void;
  onDelete: () => void;
}) {
  const [pw, setPw] = useState("");
  const [editCats, setEditCats] = useState(false);
  const [editPerms, setEditPerms] = useState(false);
  const confirm = useConfirm();
  const pending = u.approval_status === "pending";
  return (
    <Card className="p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{u.username}</span>
            {isMe && <Badge tone="violet">you</Badge>}
            <Badge tone={u.role === "admin" ? "amber" : "default"}>{u.role}</Badge>
            {pending && <Badge tone="amber">Pending approval</Badge>}
            {!u.is_active && <Badge tone="red">disabled</Badge>}
            {u.role !== "admin" && u.permissions != null && (
              <Badge tone="violet">{u.permissions.length} perm{u.permissions.length === 1 ? "" : "s"}</Badge>
            )}
            {u.role !== "admin" && u.allowed_categories != null && (
              <Badge tone="violet">{u.allowed_categories.length} categor{u.allowed_categories.length === 1 ? "y" : "ies"}</Badge>
            )}
          </div>
          {u.email && <div className="mt-0.5 truncate text-xs text-muted">{u.email}</div>}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1">
          {pending && (
            <>
              <Button size="sm" variant="primary" onClick={onApprove}>Approve</Button>
              <Button size="sm" variant="danger" onClick={onReject}>Reject</Button>
            </>
          )}
          {u.role !== "admin" && (
            <Button size="sm" variant="ghost" onClick={() => setEditPerms((v) => !v)}>
              Permissions
            </Button>
          )}
          {u.role !== "admin" && (
            <Button size="sm" variant="ghost" onClick={() => setEditCats((v) => !v)}>
              Categories
            </Button>
          )}
          <Button size="sm" variant="ghost" disabled={isMe}
            onClick={async () => {
              const toAdmin = u.role !== "admin";
              if (await confirm({
                title: toAdmin ? "Grant admin" : "Revoke admin",
                message: toAdmin
                  ? `Make “${u.username}” an admin? They'll get full control of this instance.`
                  : `Demote “${u.username}” to a regular user?`,
                danger: toAdmin,
              })) onChange({ role: toAdmin ? "admin" : "user" });
            }}>
            {u.role === "admin" ? "Make user" : "Make admin"}
          </Button>
          <Button size="sm" variant="ghost" disabled={isMe}
            onClick={async () => {
              if (await confirm({
                title: u.is_active ? "Disable user" : "Enable user",
                message: u.is_active
                  ? `Disable “${u.username}”? They won't be able to sign in.`
                  : `Re-enable “${u.username}”?`,
                danger: u.is_active,
              })) onChange({ is_active: !u.is_active });
            }}>
            {u.is_active ? "Disable" : "Enable"}
          </Button>
          <Button size="sm" variant="danger" disabled={isMe} onClick={onDelete}>✕</Button>
        </div>
      </div>

      {u.role === "admin" ? null : editPerms && (
        <div className="mt-3 rounded-lg border border-border bg-surface-2/40 p-3">
          <div className="mb-1 text-xs uppercase tracking-wide text-muted">
            Permissions for {u.username}
          </div>
          <PermissionPicker
            value={u.permissions}
            inheritLabel="Inherit the default for normal users"
            onChange={(v) => onChange({ permissions: v })}
          />
        </div>
      )}

      {u.role === "admin" ? null : editCats && (
        <div className="mt-3 rounded-lg border border-border bg-surface-2/40 p-3">
          <div className="mb-1 text-xs uppercase tracking-wide text-muted">
            Viewable categories for {u.username}
          </div>
          <CategoryPicker
            value={u.allowed_categories}
            inheritLabel="Inherit the default for normal users"
            onChange={(v) => onChange({ allowed_categories: v })}
          />
        </div>
      )}

      <div className="mt-2 flex items-center gap-2">
        <input value={pw} onChange={(e) => setPw(e.target.value)} type="password"
          placeholder="set new password"
          className="flex-1 rounded-lg border border-border bg-bg px-3 py-1.5 text-sm" />
        <Button size="sm" variant="outline" disabled={pw.length < 4}
          onClick={() => { onChange({ password: pw }); setPw(""); }}>
          Set password
        </Button>
      </div>
    </Card>
  );
}
