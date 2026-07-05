import { type ReactNode, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
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
  const { t } = useTranslation();
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
        title={t("users.registration.title")}
        desc={t("users.registration.desc")}
        hint={<><b>{t("users.registration.closedName")}</b>{t("users.registration.closedHint")} <b>{t("users.registration.openName")}</b>{t("users.registration.openHint")} <b>{t("users.registration.approvalName")}</b>{t("users.registration.approvalHint")}</>}
      />
      {cfg.isLoading ? (
        <Spinner label={t("common.loading")} />
      ) : (
        <Select
          value={mode}
          onChange={(v) => save.mutate(v as RegistrationMode)}
          options={[
            { value: "closed", label: t("users.registration.closedOption") },
            { value: "open", label: t("users.registration.openOption") },
            { value: "approval", label: t("users.registration.approvalOption") },
          ]}
        />
      )}
      {save.isPending && <p className="mt-1 text-xs text-accent">{t("common.saving")}</p>}
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
  const { t } = useTranslation();
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
        title={t("users.defaultCategories.title")}
        desc={t("users.defaultCategories.desc")}
        hint={t("users.defaultCategories.hint")}
      />
      {def.isLoading ? (
        <Spinner label={t("common.loading")} />
      ) : (
        <CategoryPicker
          value={value}
          inheritLabel={t("users.defaultCategories.allLabel")}
          onChange={(v) => save.mutate(v)}
        />
      )}
      {save.isPending && <p className="mt-1 text-xs text-accent">{t("common.saving")}</p>}
    </Card>
  );
}

/** Admin: the global 18+ gate — which categories MAY surface adult content at all. Off by default;
 *  even where enabled, each user must still opt in for themselves under their own settings. */
function AdultGateCard() {
  const { t } = useTranslation();
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
        title={t("users.adultGate.title")}
        badge={<Badge tone="red">18+</Badge>}
        desc={t("users.adultGate.desc")}
        hint={t("users.adultGate.hint")}
      />
      {gate.isLoading ? (
        <Spinner label={t("common.loading")} />
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
      {save.isPending && <p className="mt-1 text-xs text-accent">{t("common.saving")}</p>}
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
  const { t } = useTranslation();
  const qc = useQueryClient();
  const meta = useQuery({ queryKey: qk.permissionsMeta(), queryFn: api.getPermissionsMeta });
  const save = useMutation({
    mutationFn: (perms: string[] | null) => api.setPermissionDefault(perms),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.permissionsMeta() }),
  });
  return (
    <Card className="mb-4 p-4">
      <CardHeader
        title={t("users.defaultPermissions.title")}
        desc={t("users.defaultPermissions.desc")}
        hint={t("users.defaultPermissions.hint")}
      />
      {meta.isLoading ? (
        <Spinner label={t("common.loading")} />
      ) : (
        <PermissionPicker
          value={meta.data?.default ?? []}
          inheritLabel={t("users.defaultPermissions.resetLabel")}
          onChange={(v) => save.mutate(v)}
        />
      )}
      {save.isPending && <p className="mt-1 text-xs text-accent">{t("common.saving")}</p>}
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
function fmtAgo(t: TFunction, iso?: string | null): string {
  if (!iso) return t("users.ago.never");
  const d = new Date(iso);
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return t("users.ago.justNow");
  if (s < 3600) return t("users.ago.minutes", { n: Math.floor(s / 60) });
  if (s < 86400) return t("users.ago.hours", { n: Math.floor(s / 3600) });
  if (s < 604800) return t("users.ago.days", { n: Math.floor(s / 86400) });
  return d.toLocaleDateString();
}

/** The admin "defaults" cards, collapsed by default so they don't dominate the page above the
 *  actual user list. */
function DefaultsSection() {
  const { t } = useTranslation();
  return (
    <Disclosure
      title={t("users.defaultsSection.title")}
      subtitle={t("users.defaultsSection.subtitle")}
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
  const { t } = useTranslation();
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
          <h2 className="font-display text-xl font-semibold text-text">{t("users.title")}</h2>
          <p className="mt-0.5 text-sm text-muted">{t("users.subtitle")}</p>
        </div>
        <Button variant="primary" className="shrink-0" onClick={() => setDrawer({ mode: "create" })}>{t("users.addUser")}</Button>
      </div>

      <DefaultsSection />

      {pendingCount > 0 && statusF !== "pending" && (
        <button
          type="button"
          onClick={() => setStatusF("pending")}
          className="mb-4 flex w-full items-center justify-between gap-3 rounded-2xl border border-[var(--hair,var(--border))] bg-[color-mix(in_srgb,var(--accent)_5%,var(--surface))] px-4 py-3 text-left transition hover:bg-surface-2"
        >
          <span className="flex items-center gap-2.5">
            <StatusChip tone="warning">{t("users.pendingCount", { count: pendingCount })}</StatusChip>
            <span className="text-sm text-text">
              {t("users.awaitingApproval", { count: pendingCount })}
            </span>
          </span>
          <span className="shrink-0 text-xs font-semibold text-accent">{t("users.review")}</span>
        </button>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("users.searchPlaceholder")}
          className={`${inputCls} min-w-[12rem] flex-1`}
        />
        <SegmentedControl
          ariaLabel={t("users.filterByRole")}
          value={roleF}
          onChange={setRoleF}
          options={[
            { value: "all", label: t("users.roleAll") },
            { value: "admin", label: t("users.roleAdmins") },
            { value: "user", label: t("users.roleUsers") },
          ]}
        />
        <SegmentedControl
          ariaLabel={t("users.filterByStatus")}
          value={statusF}
          onChange={setStatusF}
          options={[
            { value: "all", label: t("users.statusAny") },
            { value: "active", label: t("users.statusActive") },
            { value: "disabled", label: t("users.statusDisabled") },
            { value: "pending", label: t("users.statusPending") },
          ]}
        />
      </div>

      {users.isLoading ? (
        <Spinner label={t("users.loadingUsers")} />
      ) : all.length === 0 ? (
        <EmptyState title={t("users.emptyTitle")} hint={t("users.emptyHint")} />
      ) : filtered.length === 0 ? (
        <EmptyState title={t("users.noMatchTitle")} hint={t("users.noMatchHint")} />
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
                    {u.id === meUser?.id && <StatusChip tone="violet">{t("users.chip.you")}</StatusChip>}
                    {u.role === "admin" && <StatusChip tone="accent">{t("users.chip.admin")}</StatusChip>}
                    {u.approval_status === "pending" && <StatusChip tone="warning">{t("users.chip.pending")}</StatusChip>}
                    {!u.is_active && <StatusChip tone="danger">{t("users.chip.disabled")}</StatusChip>}
                  </div>
                  <div className="truncate text-xs text-muted">
                    {u.display_name ? `@${u.username}` : ""}
                    {u.email ? `${u.display_name ? " · " : ""}${u.email}` : ""}
                  </div>
                </div>
                <div className="shrink-0 text-right text-xs text-muted">
                  <div>{fmtAgo(t, u.last_seen)}</div>
                  {!!u.active_sessions && (
                    <div>{t("users.sessions", { count: u.active_sessions })}</div>
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
  const { t } = useTranslation();
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
  const [sendInvite, setSendInvite] = useState(false);   // email the new user their sign-in details
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
        send_invite: sendInvite && !!email.trim(),
      }),
    onSuccess: () => {
      refresh();
      toast(sendInvite && email.trim()
        ? t("users.drawer.createdInviteToast", { name: username.trim(), email: email.trim() })
        : t("users.drawer.createdToast", { name: username.trim() }), "success");
      props.onClose();
    },
    onError: (e) => setErr((e as Error).message),
  });

  const title = isCreate ? t("users.drawer.addTitle") : (u!.display_name || u!.username);

  return (
    <Modal
      title={title}
      variant="fullscreen-sheet"
      width="max-w-lg"
      onClose={props.onClose}
      footer={
        isCreate ? (
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={props.onClose}>{t("common.cancel")}</Button>
            <Button variant="primary" disabled={!username.trim() || pw.length < 8 || create.isPending}
              onClick={() => create.mutate()}>
              {create.isPending ? t("users.drawer.creating") : t("users.drawer.createUser")}
            </Button>
          </div>
        ) : (
          <div className="flex justify-between gap-2">
            <Button
              variant="danger"
              disabled={isMe}
              title={isMe ? t("users.drawer.cantDeleteSelf") : undefined}
              onClick={async () => {
                deleteSecret.current = "";
                const ok = await confirm({
                  title: t("users.drawer.deleteTitle"),
                  danger: true,
                  confirmText: t("users.drawer.deleteConfirmText"),
                  message: deleteProtected ? (
                    <div className="space-y-2.5">
                      <p>{t("users.drawer.deleteProtectedLine", { name: u!.username })}</p>
                      <p className="text-xs">{t("users.drawer.deleteProtectedHint")}</p>
                      <input
                        type="password"
                        autoFocus
                        placeholder={t("users.drawer.deleteSecretPlaceholder")}
                        className={inputCls}
                        onChange={(e) => { deleteSecret.current = e.target.value; }}
                      />
                    </div>
                  ) : t("users.drawer.deleteConfirm", { name: u!.username }),
                });
                if (ok) {
                  const done = await run(
                    api.deleteUser(u!.id, deleteProtected ? deleteSecret.current : undefined),
                    t("users.drawer.deletedToast"),
                  );
                  if (done) props.onClose();   // keep the drawer open on a wrong-secret 403 (err shown)
                }
              }}
            >
              {t("users.drawer.deleteUser")}
            </Button>
            <Button variant="ghost" onClick={props.onClose}>{t("common.done")}</Button>
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
              {isMe && <StatusChip tone="violet">{t("users.chip.you")}</StatusChip>}
              {u!.role === "admin" && <StatusChip tone="accent">{t("users.chip.admin")}</StatusChip>}
              {u!.approval_status === "pending" && <StatusChip tone="warning">{t("users.chip.pending")}</StatusChip>}
              {!u!.is_active && <StatusChip tone="danger">{t("users.chip.disabled")}</StatusChip>}
            </div>
          </div>
        </div>
      )}

      <div className="space-y-4">
        <Section title={t("users.drawer.identity")}>
          <FormField label={t("users.drawer.username")} hint={isCreate ? undefined : t("users.drawer.usernameHint")}>
            <input className={inputCls} value={username} onChange={(e) => setUsername(e.target.value)}
              placeholder={t("users.drawer.usernamePlaceholder")} autoFocus={isCreate} />
          </FormField>
          <FormField label={t("users.drawer.displayName")}>
            <input className={inputCls} value={displayName} onChange={(e) => setDisplayName(e.target.value)}
              placeholder={isCreate ? t("users.drawer.optional") : u!.username} />
          </FormField>
          <FormField label={t("users.drawer.email")} hint={t("users.drawer.emailHint")}>
            <input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)}
              placeholder={t("users.drawer.optional")} />
          </FormField>
          {isCreate && (
            <label className={`flex items-start gap-2 text-sm ${email.trim() ? "cursor-pointer text-text" : "cursor-not-allowed text-muted"}`}>
              <input
                type="checkbox"
                className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
                checked={sendInvite && !!email.trim()}
                disabled={!email.trim()}
                onChange={(e) => setSendInvite(e.target.checked)}
              />
              <span>
                {t("users.drawer.sendInvite")}
                <span className="mt-0.5 block text-xs text-muted">
                  {email.trim() ? t("users.drawer.sendInviteHint") : t("users.drawer.sendInviteNeedEmail")}
                </span>
              </span>
            </label>
          )}
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
                  t("users.drawer.profileSaved")
                )
              }
            >
              {t("users.drawer.saveProfile")}
            </Button>
          )}
        </Section>

        {/* Role + categories/permissions. Admins implicitly hold everything, so the pickers only
            show for regular users. */}
        <Section title={t("users.drawer.roleAccess")}>
          {isCreate ? (
            <FormField label={t("users.drawer.role")}>
              <Select value={role} onChange={setRole}
                options={[{ value: "user", label: t("users.drawer.roleUser") }, { value: "admin", label: t("users.drawer.roleAdmin") }]} />
            </FormField>
          ) : (
            <div className="mb-3.5 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-sm">
                <span className="text-muted">{t("users.drawer.role")}</span>
                <StatusChip tone={u!.role === "admin" ? "accent" : "neutral"}>{u!.role}</StatusChip>
              </div>
              <Button
                size="sm"
                variant="outline"
                disabled={isMe || busy}
                title={isMe ? t("users.drawer.cantChangeOwnRole") : undefined}
                onClick={async () => {
                  const toAdmin = u!.role !== "admin";
                  if (
                    await confirm({
                      title: toAdmin ? t("users.drawer.grantAdmin") : t("users.drawer.revokeAdmin"),
                      message: toAdmin
                        ? t("users.drawer.grantAdminConfirm", { name: u!.username })
                        : t("users.drawer.revokeAdminConfirm", { name: u!.username }),
                      danger: toAdmin,
                    })
                  )
                    run(api.updateUser(u!.id, { role: toAdmin ? "admin" : "user" }));
                }}
              >
                {u!.role === "admin" ? t("users.drawer.makeUser") : t("users.drawer.makeAdmin")}
              </Button>
            </div>
          )}

          {/* Edit mode reads LIVE u!.role (a "Make admin" with the drawer open flips it without a
              re-seed); create mode uses local role state. Keeps the pickers' visibility correct. */}
          {(isCreate ? role : u!.role) !== "admin" && (
            <div className="grid gap-4">
              <div>
                <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{t("users.drawer.permissions")}</div>
                {/* Edit mode reads the LIVE user so a server-normalized value (or a concurrent
                    change) is reflected; create mode uses local state seeded to null. */}
                <PermissionPicker
                  value={isCreate ? perms : u!.permissions}
                  inheritLabel={t("users.drawer.inheritDefault")}
                  onChange={(v) => {
                    if (isCreate) setPerms(v);
                    else run(api.updateUser(u!.id, { permissions: v }));
                  }}
                />
              </div>
              <div>
                <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{t("users.drawer.viewableCategories")}</div>
                <CategoryPicker
                  value={isCreate ? cats : u!.allowed_categories}
                  inheritLabel={t("users.drawer.inheritDefault")}
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
        <Section title={isCreate ? t("users.drawer.password") : t("users.drawer.setNewPassword")}>
          <input className={inputCls} type="password" value={pw} onChange={(e) => setPw(e.target.value)}
            placeholder={t("users.drawer.passwordPlaceholder")} autoComplete="new-password" />
          {!isCreate && (
            <Button className="mt-2.5" size="sm" variant="outline" disabled={pw.length < 8 || busy}
              onClick={() => run(api.updateUser(u!.id, { password: pw }), t("users.drawer.passwordUpdated")).then(() => setPw(""))}>
              {t("users.drawer.setPassword")}
            </Button>
          )}
        </Section>

        {/* Account actions (edit only) */}
        {!isCreate && (
          <Section title={t("users.drawer.dangerZone")} tone="danger">
            {u!.approval_status === "pending" && (
              <div className="mb-3 flex gap-2">
                <Button size="sm" variant="primary" disabled={busy} onClick={() => run(api.approveUser(u!.id), t("users.drawer.userApproved"))}>
                  {t("users.drawer.approve")}
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={async () => {
                    if (await confirm({ title: t("users.drawer.rejectTitle"), message: t("users.drawer.rejectConfirm", { name: u!.username }), danger: true, confirmText: t("users.drawer.reject") })) {
                      await run(api.rejectUser(u!.id), t("users.drawer.registrationRejected"));
                      props.onClose();
                    }
                  }}
                >
                  {t("users.drawer.reject")}
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
                      title: u!.is_active ? t("users.drawer.disableTitle") : t("users.drawer.enableTitle"),
                      message: u!.is_active
                        ? t("users.drawer.disableConfirm", { name: u!.username })
                        : t("users.drawer.enableConfirm", { name: u!.username }),
                      danger: u!.is_active,
                    })
                  )
                    run(api.updateUser(u!.id, { is_active: !u!.is_active }));
                }}
              >
                {u!.is_active ? t("users.drawer.disableAccount") : t("users.drawer.enableAccount")}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={isMe || busy || !u!.active_sessions}
                title={
                  isMe
                    ? t("users.drawer.cantSignSelfOut")
                    : u!.active_sessions
                      ? t("users.drawer.signOutSessions", { count: u!.active_sessions })
                      : t("users.drawer.noActiveSessions")
                }
                onClick={async () => {
                  if (await confirm({ title: t("users.drawer.logoutEverywhereTitle"), message: t("users.drawer.logoutEverywhereConfirm", { name: u!.username }), confirmText: t("users.drawer.logout") }))
                    run(api.logoutAllSessions(u!.id), t("users.drawer.signedOutEverywhere"));
                }}
              >
                {t("users.drawer.logoutEverywhere")}
              </Button>
            </div>
            <p className="mt-3 text-xs text-muted">
              {t("users.drawer.lastSignIn", { ago: fmtAgo(t, u!.last_seen) })}
              {u!.active_sessions ? t("users.drawer.activeSessionsSuffix", { count: u!.active_sessions }) : ""}.
            </p>
          </Section>
        )}
      </div>
    </Modal>
  );
}
