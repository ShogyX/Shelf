import React, { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, RegistrationMode } from "../api/client";
import { useAuth } from "../auth";
import { Button, inputCls, Spinner } from "./ui";

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
    <main
      className="flex min-h-full items-center justify-center px-4 py-12"
      style={{ background: "var(--ambient, var(--bg))" }}
    >
      <div className="w-full max-w-sm rounded-[20px] border border-[var(--hair-strong,var(--border))] bg-surface p-6 shadow-[var(--pop-shadow)]">
        <div className="mb-5 text-center">
          <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-accent/10 text-3xl">📚</div>
          <h1 className="font-display mt-3 text-2xl font-semibold tracking-tight">{title}</h1>
          <p className="mt-1.5 text-sm text-[var(--text-soft,var(--muted))]">{subtitle}</p>
        </div>
        {children}
      </div>
    </main>
  );
}

function Field(props: React.InputHTMLAttributes<HTMLInputElement> & { label: string }) {
  const { label, ...rest } = props;
  return (
    <label className="block">
      <span className="mb-1.5 block text-[13px] font-semibold text-text">{label}</span>
      <input {...rest} className={inputCls} />
    </label>
  );
}

export function AuthSpinner() {
  const { t } = useTranslation();
  return (
    <main className="flex min-h-full items-center justify-center">
      <Spinner label={t("common.loading")} />
    </main>
  );
}

export function Login() {
  const { t } = useTranslation();
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
        setErr((e as Error).message || t("auth.loginFailed"));
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <Shell title={t("auth.signInTitle")} subtitle={t("auth.signInSubtitle")}>
      <form onSubmit={submit} className="space-y-3">
        <Field label={t("auth.username")} value={username} onChange={(e) => setU(e.target.value)}
          autoFocus autoComplete="username" />
        <Field label={t("auth.password")} type="password" value={password} onChange={(e) => setP(e.target.value)}
          autoComplete="current-password" />
        {pending && (
          <p className="rounded-lg border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-sm text-amber-600 dark:text-amber-400">
            {t("auth.pendingApproval")}
          </p>
        )}
        {err && (
          <p className="rounded-lg border border-red-400/25 bg-red-500/10 px-3 py-2 text-sm text-red-500">{err}</p>
        )}
        <Button variant="primary" className="w-full justify-center" disabled={busy || !username || !password}>
          {busy ? t("auth.signingIn") : t("auth.signIn")}
        </Button>
      </form>
      <div className="mt-4 flex items-center justify-between text-sm">
        {mode !== "loading" && mode !== "closed" ? (
          <Link to="/register" className="text-accent hover:underline">{t("auth.createAccount")}</Link>
        ) : (
          <span />
        )}
        <Link to="/forgot" className="text-muted hover:text-text hover:underline">{t("auth.forgotPassword")}</Link>
      </div>
    </Shell>
  );
}

export function Register() {
  const { t } = useTranslation();
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
      if (status === 409) setErr(t("auth.takenError"));
      else if (status === 422) setErr(t("auth.invalidEmailError"));
      else if (status === 400) setErr(ae.message || t("auth.passwordTooShort"));
      else setErr(ae.message || t("auth.registrationFailed"));
    } finally {
      setBusy(false);
    }
  }

  if (mode === "loading") return <AuthSpinner />;

  if (mode === "closed") {
    return (
      <Shell title={t("auth.registrationDisabledTitle")} subtitle={t("auth.registrationDisabledSubtitle")}>
        <p className="text-sm text-muted">
          {t("auth.registrationDisabledBody")}
        </p>
        <div className="mt-4 text-center text-sm">
          <Link to="/login" className="text-accent hover:underline">{t("auth.backToSignIn")}</Link>
        </div>
      </Shell>
    );
  }

  if (pending) {
    return (
      <Shell title={t("auth.almostThereTitle")} subtitle={t("auth.almostThereSubtitle")}>
        <p className="rounded-lg border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-sm text-amber-600 dark:text-amber-400">
          {t("auth.registrationPending")}
        </p>
        <div className="mt-4 text-center text-sm">
          <Link to="/login" className="text-accent hover:underline">{t("auth.backToSignIn")}</Link>
        </div>
      </Shell>
    );
  }

  return (
    <Shell title={t("auth.registerTitle")} subtitle={t("auth.registerSubtitle")}>
      <form onSubmit={submit} className="space-y-3">
        <Field label={t("auth.username")} value={username} onChange={(e) => setU(e.target.value)}
          autoFocus autoComplete="username" />
        <Field label={t("auth.email")} type="email" value={email} onChange={(e) => setE(e.target.value)}
          autoComplete="email" />
        <Field label={t("auth.password")} type="password" value={password} onChange={(e) => setP(e.target.value)}
          autoComplete="new-password" />
        <div>
          <Field label={t("auth.kindleEmailOptional")} type="email" value={kindleEmail}
            onChange={(e) => setK(e.target.value)} placeholder="device@kindle.com" autoComplete="off" />
          <p className="mt-1 text-xs text-[var(--text-soft,var(--muted))]">{t("auth.kindleEmailHint")}</p>
        </div>
        {err && (
          <p className="rounded-lg border border-red-400/25 bg-red-500/10 px-3 py-2 text-sm text-red-500">{err}</p>
        )}
        <Button variant="primary" className="w-full justify-center"
          disabled={busy || !username || !email || !password}>
          {busy ? t("auth.creating") : t("auth.createAccountBtn")}
        </Button>
      </form>
      <div className="mt-4 text-center text-sm">
        <Link to="/login" className="text-muted hover:text-text hover:underline">{t("auth.alreadyHaveAccount")}</Link>
      </div>
    </Shell>
  );
}

export function Forgot() {
  const { t } = useTranslation();
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
      <Shell title={t("auth.checkInboxTitle")} subtitle={t("auth.checkInboxSubtitle")}>
        <p className="text-sm text-muted">
          {t("auth.resetLinkSent")}
        </p>
        <div className="mt-4 text-center text-sm">
          <Link to="/login" className="text-accent hover:underline">{t("auth.backToSignIn")}</Link>
        </div>
      </Shell>
    );
  }

  return (
    <Shell title={t("auth.forgotTitle")} subtitle={t("auth.forgotSubtitle")}>
      <form onSubmit={submit} className="space-y-3">
        <Field label={t("auth.usernameOrEmail")} value={identifier} onChange={(e) => setId(e.target.value)}
          autoFocus autoComplete="username" />
        <Button variant="primary" className="w-full justify-center" disabled={busy || !identifier.trim()}>
          {busy ? t("auth.sending") : t("auth.sendResetLink")}
        </Button>
      </form>
      <div className="mt-4 text-center text-sm">
        <Link to="/login" className="text-muted hover:text-text hover:underline">{t("auth.backToSignIn")}</Link>
      </div>
    </Shell>
  );
}

export function Reset() {
  const { t } = useTranslation();
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
      setErr(t("auth.passwordsDontMatch"));
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
        setErr(t("auth.resetLinkInvalid"));
      } else {
        setErr(ae.message || t("auth.resetFailed"));
      }
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <Shell title={t("auth.passwordUpdatedTitle")} subtitle={t("auth.passwordUpdatedSubtitle")}>
        <p className="text-sm text-muted">{t("auth.passwordChangedBody")}</p>
        <div className="mt-4 text-center text-sm">
          <Link to="/login" className="text-accent hover:underline">{t("auth.signIn")}</Link>
        </div>
      </Shell>
    );
  }

  if (noToken) {
    return (
      <Shell title={t("auth.resetTitle")} subtitle={t("auth.resetSubtitle")}>
        <p className="text-sm text-red-500">{t("auth.resetLinkInvalid")}</p>
        <div className="mt-4 text-center text-sm">
          <Link to="/forgot" className="text-accent hover:underline">{t("auth.requestNewLink")}</Link>
        </div>
      </Shell>
    );
  }

  return (
    <Shell title={t("auth.resetTitle")} subtitle={t("auth.resetSubtitle")}>
      <form onSubmit={submit} className="space-y-3">
        <Field label={t("auth.newPassword")} type="password" value={password} onChange={(e) => setP(e.target.value)}
          autoFocus autoComplete="new-password" />
        <Field label={t("auth.confirmPassword")} type="password" value={confirm} onChange={(e) => setC(e.target.value)}
          autoComplete="new-password" />
        {err && (
          <p className="rounded-lg border border-red-400/25 bg-red-500/10 px-3 py-2 text-sm text-red-500">{err}</p>
        )}
        <Button variant="primary" className="w-full justify-center"
          disabled={busy || password.length < 4 || !confirm}>
          {busy ? t("auth.updating") : t("auth.updatePassword")}
        </Button>
      </form>
    </Shell>
  );
}

export function Setup() {
  const { t } = useTranslation();
  const { refresh } = useAuth();
  const [username, setU] = useState("");
  const [password, setP] = useState("");
  const [confirm, setC] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setErr(t("auth.passwordsDontMatch"));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.setupAdmin(username.trim(), password);
      await refresh();
    } catch (e) {
      setErr((e as Error).message || t("auth.setupFailed"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Shell title={t("auth.setupTitle")} subtitle={t("auth.setupSubtitle")}>
      <form onSubmit={submit} className="space-y-3">
        <Field label={t("auth.adminUsername")} value={username} onChange={(e) => setU(e.target.value)}
          autoFocus autoComplete="username" />
        <Field label={t("auth.password")} type="password" value={password} onChange={(e) => setP(e.target.value)}
          autoComplete="new-password" />
        <Field label={t("auth.confirmPassword")} type="password" value={confirm} onChange={(e) => setC(e.target.value)}
          autoComplete="new-password" />
        {err && (
          <p className="rounded-lg border border-red-400/25 bg-red-500/10 px-3 py-2 text-sm text-red-500">{err}</p>
        )}
        <Button variant="primary" className="w-full justify-center"
          disabled={busy || !username || password.length < 4}>
          {busy ? t("auth.creating") : t("auth.createAdmin")}
        </Button>
      </form>
    </Shell>
  );
}
