import React, { useState } from "react";
import { api } from "../api/client";
import { useAuth } from "../auth";
import { Button, Card, Spinner } from "./ui";

function Shell({ title, subtitle, children }: {
  title: string; subtitle: string; children: React.ReactNode;
}) {
  return (
    <main className="flex min-h-full items-center justify-center px-4 py-12">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-5 text-center">
          <div className="text-3xl">📚</div>
          <h1 className="mt-2 text-xl font-semibold">{title}</h1>
          <p className="mt-1 text-sm text-muted">{subtitle}</p>
        </div>
        {children}
      </Card>
    </main>
  );
}

function Field(props: React.InputHTMLAttributes<HTMLInputElement> & { label: string }) {
  const { label, ...rest } = props;
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium">{label}</span>
      <input
        {...rest}
        className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-sm focus:border-accent focus:outline-none"
      />
    </label>
  );
}

export function AuthSpinner() {
  return (
    <main className="flex min-h-full items-center justify-center">
      <Spinner label="Loading…" />
    </main>
  );
}

export function Login() {
  const { refresh } = useAuth();
  const [username, setU] = useState("");
  const [password, setP] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.login(username.trim(), password);
      await refresh();
    } catch (e) {
      setErr((e as Error).message || "Login failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Shell title="Sign in to Shelf" subtitle="Enter your account credentials">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Username" value={username} onChange={(e) => setU(e.target.value)}
          autoFocus autoComplete="username" />
        <Field label="Password" type="password" value={password} onChange={(e) => setP(e.target.value)}
          autoComplete="current-password" />
        {err && <p className="text-sm text-red-500">{err}</p>}
        <Button variant="primary" className="w-full justify-center" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </Shell>
  );
}

export function Setup() {
  const { refresh } = useAuth();
  const [username, setU] = useState("");
  const [password, setP] = useState("");
  const [confirm, setC] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setErr("Passwords don't match");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.setupAdmin(username.trim(), password);
      await refresh();
    } catch (e) {
      setErr((e as Error).message || "Setup failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Shell title="Welcome to Shelf" subtitle="Create the administrator account to get started">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Admin username" value={username} onChange={(e) => setU(e.target.value)}
          autoFocus autoComplete="username" />
        <Field label="Password" type="password" value={password} onChange={(e) => setP(e.target.value)}
          autoComplete="new-password" />
        <Field label="Confirm password" type="password" value={confirm} onChange={(e) => setC(e.target.value)}
          autoComplete="new-password" />
        {err && <p className="text-sm text-red-500">{err}</p>}
        <Button variant="primary" className="w-full justify-center"
          disabled={busy || !username || password.length < 4}>
          {busy ? "Creating…" : "Create admin & continue"}
        </Button>
      </form>
    </Shell>
  );
}
