import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, User } from "../api/client";
import { useCurrentUser } from "../auth";
import { Badge, Button, Card, EmptyState, Spinner } from "../components/ui";

export default function Users() {
  const qc = useQueryClient();
  const meUser = useCurrentUser();
  const users = useQuery({ queryKey: ["users"], queryFn: api.listUsers });
  const [error, setError] = useState<string | null>(null);

  const [nu, setNu] = useState("");
  const [np, setNp] = useState("");
  const [nrole, setNrole] = useState("user");

  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });
  const wrap = (p: Promise<unknown>) => p.then(refresh).catch((e) => setError((e as Error).message));

  const create = useMutation({
    mutationFn: () => api.createUser({ username: nu.trim(), password: np, role: nrole }),
    onSuccess: () => { setNu(""); setNp(""); setNrole("user"); setError(null); refresh(); },
    onError: (e) => setError((e as Error).message),
  });

  return (
    <main className="mx-auto max-w-3xl px-4 py-8">
      <h1 className="mb-1 text-2xl font-semibold">Users</h1>
      <p className="mb-6 text-sm text-muted">
        Everyone shares the same library; reading progress and settings are private to each account.
      </p>

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
              onDelete={() => { if (confirm(`Delete user "${u.username}"?`)) wrap(api.deleteUser(u.id)); }} />
          ))}
        </div>
      )}
    </main>
  );
}

function UserRow({ u, isMe, onChange, onDelete }: {
  u: User; isMe: boolean;
  onChange: (patch: { role?: string; is_active?: boolean; password?: string }) => void;
  onDelete: () => void;
}) {
  const [pw, setPw] = useState("");
  return (
    <Card className="p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate font-medium">{u.username}</span>
            {isMe && <Badge tone="violet">you</Badge>}
            <Badge tone={u.role === "admin" ? "amber" : "default"}>{u.role}</Badge>
            {!u.is_active && <Badge tone="red">disabled</Badge>}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1">
          <Button size="sm" variant="ghost" disabled={isMe}
            onClick={() => onChange({ role: u.role === "admin" ? "user" : "admin" })}>
            {u.role === "admin" ? "Make user" : "Make admin"}
          </Button>
          <Button size="sm" variant="ghost" disabled={isMe}
            onClick={() => onChange({ is_active: !u.is_active })}>
            {u.is_active ? "Disable" : "Enable"}
          </Button>
          <Button size="sm" variant="danger" disabled={isMe} onClick={onDelete}>✕</Button>
        </div>
      </div>
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
