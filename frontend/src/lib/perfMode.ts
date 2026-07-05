// Device-aware "Performance mode". When ON, the app drops its GPU-heavy decoration — the animated
// ambient aurora (a perpetual rAF repaint), the nav/reader/player `backdrop-filter` blurs, the film
// grain, the 3D poster tilt and the equalizer animation — by flagging `<html data-fx="lite">`
// (consumed in index.css) and stopping the ambient motion loop. The page keeps its look (static
// aurora, opaque nav) but stops keeping a discrete GPU busy at idle.
//
// It's a PER-DEVICE choice (localStorage), not a per-account setting: the same user's laptop (on
// battery) and desktop want different answers. Unset → auto-detected from low-power signals.
import { setAmbientEnabled } from "./coverBackdrop";

const KEY = "shelf-perf-mode"; // "on" | "off" | absent (auto)

// Heuristic default when the user hasn't chosen: LITE on signals of a low-power / laptop / battery /
// data-saving / reduced-motion device (e.g. an Acer Nitro on battery), FULL effects otherwise.
function detectDefault(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const mm = window.matchMedia?.bind(window);
    if (mm?.("(prefers-reduced-motion: reduce)").matches) return true;
    if (mm?.("(update: slow)").matches) return true;
    const nav = navigator as unknown as {
      connection?: { saveData?: boolean };
      deviceMemory?: number;
      hardwareConcurrency?: number;
    };
    if (nav.connection?.saveData) return true;
    if (typeof nav.deviceMemory === "number" && nav.deviceMemory <= 4) return true;
    if (typeof nav.hardwareConcurrency === "number" && nav.hardwareConcurrency <= 4) return true;
  } catch {
    /* matchMedia / navigator unavailable — fall through to full effects */
  }
  return false;
}

export function isPerfModeExplicit(): boolean {
  try {
    return localStorage.getItem(KEY) != null;
  } catch {
    return false;
  }
}

export function perfModeEnabled(): boolean {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "on") return true;
    if (v === "off") return false;
  } catch {
    /* storage unavailable — use the auto default */
  }
  return detectDefault();
}

// Apply the current mode to the document (flag + ambient loop). Idempotent.
export function applyPerfMode(on: boolean): void {
  const root = document.documentElement;
  if (on) root.dataset.fx = "lite";
  else delete root.dataset.fx;
  setAmbientEnabled(!on);
}

// The user's explicit toggle (persists per-device).
export function setPerfMode(on: boolean): void {
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    /* non-fatal — apply for this session anyway */
  }
  applyPerfMode(on);
}

// Boot: apply the resolved mode, then (only if the user hasn't chosen) refine the auto default with
// the async Battery API — a discharging battery is a strong "laptop on the go" signal → LITE.
export function initPerfMode(): void {
  applyPerfMode(perfModeEnabled());
  if (isPerfModeExplicit()) return;
  try {
    const getBattery = (navigator as unknown as {
      getBattery?: () => Promise<{ charging: boolean }>;
    }).getBattery;
    if (typeof getBattery === "function") {
      getBattery.call(navigator).then((bat) => {
        if (bat && bat.charging === false && !isPerfModeExplicit()) applyPerfMode(true);
      }).catch(() => { /* Battery API blocked — keep the sync default */ });
    }
  } catch {
    /* no Battery API — keep the sync default */
  }
}
