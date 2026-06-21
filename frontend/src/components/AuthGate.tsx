import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, RegistrationMode } from "../api/client";
import { useAuth } from "../auth";
import { Button, Card, Spinner } from "./ui";

// Best-effort registration-mode probe for the public auth screens. Fails SAFE: any error (e.g. an
// older backend without the endpoint) is treated as "closed" so we never offer a broken sign-up.
function useRegistrationMode(): RegistrationMode | "loading" {
  const [mode, setMode] = useState<RegistrationMode | "loading">("loading");
  useEffect(() => {
    let alive = true;
    api
      .registrationMode()
      .then((r) => alive && setMode(r.mode))
      .catch(() => alive && setMode("closed"));
    return () => {
      alive = false;
    };
  }, []);
  return mode;
}

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
  const mode = useRegistrationMode();
  const [username, setU] = useState("");
  const [password, setP] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    setPending(false);
    try {
      await api.login(username.trim(), password);
      await refresh();
    } catch (e) {
      // A pending self-registered account is rejected with 403 "account pending approval" —
      // surface that distinctly so the user knows it's awaiting an admin, not a bad password.
      if (e instanceof ApiError && e.status === 403 && /pending approval/i.test(e.message)) {
        setPending(true);
      } else {
        setErr((e as Error).message || "Login failed");
      }
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
        {pending && (
          <p className="rounded-lg bg-amber-500/15 px-3 py-2 text-sm text-amber-600 dark:text-amber-400">
            Your account is still pending admin approval.
          </p>
        )}
        {err && <p className="text-sm text-red-500">{err}</p>}
        <Button variant="primary" className="w-full justify-center" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
      <div className="mt-4 flex items-center justify-between text-sm">
        {mode !== "loading" && mode !== "closed" ? (
          <Link to="/register" className="text-accent hover:underline">Create an account</Link>
        ) : (
          <span />
        )}
        <Link to="/forgot" className="text-muted hover:text-text hover:underline">Forgot password?</Link>
      </div>
    </Shell>
  );
}

export function Register() {
  const { refresh } = useAuth();
  const mode = useRegistrationMode();
  const [username, setU] = useState("");
  const [email, setE] = useState("");
  const [password, setP] = useState("");
  const [kindleEmail, setK] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const r = await api.register({
        username: username.trim(), email: email.trim(), password,
        ...(kindleEmail.trim() ? { kindle_email: kindleEmail.trim() } : {}),
      });
      if (r.status === "pending") {
        setPending(true);
      } else {
        // Open mode: the server set the session cookie — refresh() picks it up and routes to the app.
        await refresh();
      }
    } catch (e) {
      const ae = e as ApiError;
      const status = ae instanceof ApiError ? ae.status : 0;
      if (status === 409) setErr("That username or email is already taken.");
      else if (status === 422) setErr("Please enter a valid email address.");
      else if (status === 400) setErr(ae.message || "Password is too short.");
      else setErr(ae.message || "Registration failed");
    } finally {
      setBusy(false);
    }
  }

  if (mode === "loading") return <AuthSpinner />;

  if (mode === "closed") {
    return (
      <Shell title="Registration is disabled" subtitle="This instance isn’t accepting sign-ups">
        <p className="text-sm text-muted">
          Ask an admin to create an account for you.
        </p>
        <div className="mt-4 text-center text-sm">
          <Link to="/login" className="text-accent hover:underline">Back to sign in</Link>
        </div>
      </Shell>
    );
  }

  if (pending) {
    return (
      <Shell title="Almost there" subtitle="Your registration was received">
        <p className="rounded-lg bg-amber-500/15 px-3 py-2 text-sm text-amber-600 dark:text-amber-400">
          Your account is pending admin approval. You’ll be able to sign in once it’s approved.
        </p>
        <div className="mt-4 text-center text-sm">
          <Link to="/login" className="text-accent hover:underline">Back to sign in</Link>
        </div>
      </Shell>
    );
  }

  return (
    <Shell title="Create your account" subtitle="Choose a username and a password">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Username" value={username} onChange={(e) => setU(e.target.value)}
          autoFocus autoComplete="username" />
        <Field label="Email" type="email" value={email} onChange={(e) => setE(e.target.value)}
          autoComplete="email" />
        <Field label="Password" type="password" value={password} onChange={(e) => setP(e.target.value)}
          autoComplete="new-password" />
        <div>
          <Field label="Send-to-Kindle email (optional)" type="email" value={kindleEmail}
            onChange={(e) => setK(e.target.value)} placeholder="device@kindle.com" autoComplete="off" />
          <p className="mt-1 text-xs text-muted">Where EPUBs go when you tap Send. You can add or change this later in Settings.</p>
        </div>
        {err && <p className="text-sm text-red-500">{err}</p>}
        <Button variant="primary" className="w-full justify-center"
          disabled={busy || !username || !email || !password}>
          {busy ? "Creating…" : "Create account"}
        </Button>
      </form>
      <div className="mt-4 text-center text-sm">
        <Link to="/login" className="text-muted hover:text-text hover:underline">Already have an account?</Link>
      </div>
    </Shell>
  );
}

export function Forgot() {
  const [identifier, setId] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    // Never reveal whether the account exists: the endpoint always returns 200; even a network
    // error shows the same neutral confirmation.
    try {
      await api.forgotPassword(identifier.trim());
    } catch {
      /* ignore — same neutral message either way */
    } finally {
      setBusy(false);
      setSent(true);
    }
  }

  if (sent) {
    return (
      <Shell title="Check your inbox" subtitle="Password reset">
        <p className="text-sm text-muted">
          If an account with that username or email exists, we’ve sent a reset link to it.
        </p>
        <div className="mt-4 text-center text-sm">
          <Link to="/login" className="text-accent hover:underline">Back to sign in</Link>
        </div>
      </Shell>
    );
  }

  return (
    <Shell title="Reset your password" subtitle="Enter your username or email">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Username or email" value={identifier} onChange={(e) => setId(e.target.value)}
          autoFocus autoComplete="username" />
        <Button variant="primary" className="w-full justify-center" disabled={busy || !identifier.trim()}>
          {busy ? "Sending…" : "Send reset link"}
        </Button>
      </form>
      <div className="mt-4 text-center text-sm">
        <Link to="/login" className="text-muted hover:text-text hover:underline">Back to sign in</Link>
      </div>
    </Shell>
  );
}

export function Reset() {
  const [params] = useSearchParams();
  const token = (params.get("token") ?? "").trim();
  const [password, setP] = useState("");
  const [confirm, setC] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  // A missing/blank token can never be valid — say so without calling the API.
  const noToken = !token;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setErr("Passwords don’t match");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.resetPassword(token, password);
      setDone(true);
    } catch (e) {
      const ae = e as ApiError;
      if (ae instanceof ApiError && ae.status === 400) {
        setErr("This reset link is invalid or has expired.");
      } else {
        setErr(ae.message || "Couldn’t reset your password");
      }
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <Shell title="Password updated" subtitle="You’re all set">
        <p className="text-sm text-muted">Your password has been changed.</p>
        <div className="mt-4 text-center text-sm">
          <Link to="/login" className="text-accent hover:underline">Sign in</Link>
        </div>
      </Shell>
    );
  }

  if (noToken) {
    return (
      <Shell title="Reset your password" subtitle="Choose a new password">
        <p className="text-sm text-red-500">This reset link is invalid or has expired.</p>
        <div className="mt-4 text-center text-sm">
          <Link to="/forgot" className="text-accent hover:underline">Request a new link</Link>
        </div>
      </Shell>
    );
  }

  return (
    <Shell title="Reset your password" subtitle="Choose a new password">
      <form onSubmit={submit} className="space-y-3">
        <Field label="New password" type="password" value={password} onChange={(e) => setP(e.target.value)}
          autoFocus autoComplete="new-password" />
        <Field label="Confirm password" type="password" value={confirm} onChange={(e) => setC(e.target.value)}
          autoComplete="new-password" />
        {err && <p className="text-sm text-red-500">{err}</p>}
        <Button variant="primary" className="w-full justify-center"
          disabled={busy || password.length < 4 || !confirm}>
          {busy ? "Updating…" : "Update password"}
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
