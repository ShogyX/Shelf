import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./index.css";
// Initialise i18next before the app renders so the first paint is already in the right language.
import "./i18n";
import { initPerfMode } from "./lib/perfMode";

// Apply device-aware Performance mode before first paint (sets <html data-fx="lite"> so the GPU-heavy
// blur/aurora/grain never even paint on a low-power device). Safe if storage/matchMedia are missing.
initPerfMode();

const queryClient = new QueryClient({
  // Cached-first: a longer staleTime means navigating back to a page renders its lists from cache
  // immediately (no empty-flash) instead of refetching every time; gcTime keeps that cache warm for
  // 30 min. Mutations still invalidate their keys explicitly, so edits show up regardless of
  // staleTime. refetchOnWindowFocus stays off so tab-switching doesn't blank the grid.
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, retry: 1, staleTime: 15_000, gcTime: 30 * 60_000 },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);

// PWA service worker — only in a production build served over a secure context (SWs require
// https or localhost). The dev server (Vite proxy) skips it so HMR isn't shadowed by a cache.
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* registration is best-effort — the app works fine without offline support */
    });
  });
}
