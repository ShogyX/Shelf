import { useApp } from "../store";

/** Non-blocking toast stack (replaces blocking alert() for results/errors). Fixed near the
 *  bottom, above the safe-area inset; tap a toast to dismiss it early. */
export default function Toaster() {
  const toasts = useApp((s) => s.toasts);
  const dismiss = useApp((s) => s.dismissToast);
  if (toasts.length === 0) return null;
  return (
    <div
      className="pointer-events-none fixed inset-x-0 z-[60] flex flex-col items-center gap-2 px-3"
      style={{ bottom: "max(1rem, env(safe-area-inset-bottom))" }}
    >
      {toasts.map((t) => (
        <button
          key={t.id}
          onClick={() => dismiss(t.id)}
          className={`pointer-events-auto max-w-md w-full sm:w-auto rounded-xl border px-4 py-2.5 text-left text-sm shadow-lg backdrop-blur transition ${
            t.kind === "error"
              ? "border-red-400/40 bg-red-500/15 text-red-100"
              : t.kind === "success"
              ? "border-green-400/40 bg-green-500/15 text-green-100"
              : "border-border bg-surface/95 text-text"
          }`}
          role="status"
        >
          {t.msg}
        </button>
      ))}
    </div>
  );
}
